"""財報分析的全市場掃描與建議成績單。

每個交易日收盤後，把潛力股評分的股票池（台股約 770 檔、S&P 500 + Nasdaq-100 約 510 檔）全部跑一次
financials.analyze()，結果依日期存進 fa_scan.db：
1. 選股掃描：最新一天的結果可依建議、分數、是否低於買進價篩選排序。
2. 建議成績單：拿過去每一天的建議，對照之後 1 週 / 1 個月 / 3 個月 / 6 個月的實際報酬，
   和同一天全部股票的平均比較（超額報酬、勝率）。從開始掃描那天累積，不回填過去
   ——用今天的財報回推過去的建議會偷看到未來，成績就不可信。
"""
import os
import time
import json
import fcntl
import sqlite3
import threading

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request

import financials
import picks
import tw_board
import userdata

bp = Blueprint('fa_scan', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'fa_scan.db')
LOCK_FILE = os.path.join(BASE_DIR, '.fa_scan.lock')
STATUS_FILE = os.path.join(BASE_DIR, '.fa_scan_status.json')
HORIZONS = [(5, '1 週'), (21, '1 個月'), (63, '3 個月'), (126, '6 個月')]
LEVELS = [('buy', '買進'), ('accumulate', '逢低布局'), ('hold', '持有觀望'), ('avoid', '避開')]
# Yahoo 對同一台主機有流量上限，被限流時連使用者在頁面上查個股都會失敗。
# 所以掃描只在離峰時段（晚上 8:30～隔天 8:30、週末）慢慢跑：一次一檔、每檔間隔 1 秒，
# 一遇到限流就整批暫停 20 分鐘，下次從中斷處接著跑。
PACE = 1.0
COOLDOWN = 1200
_cool_until = 0.0


class RateLimited(Exception):
    pass


def _offpeak(now):
    hm = now.hour * 100 + now.minute
    return now.weekday() >= 5 or hm >= 2030 or hm < 830


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    return con


def init_db():
    with _db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS snaps(date TEXT, market TEXT, code TEXT, name TEXT, sector TEXT, price REAL,
            score INT, quality INT, value INT, growth INT, profit INT, health INT, qual INT,
            rec TEXT, fair REAL, upside REAL, buy_below REAL, conf TEXT, flags INT, PRIMARY KEY(date, code));
        CREATE INDEX IF NOT EXISTS snaps_mkt ON snaps(market, date);
        CREATE TABLE IF NOT EXISTS runs(market TEXT, date TEXT, started TEXT, finished TEXT, ok INT, fail INT,
            PRIMARY KEY(market, date));
        ''')


# ── 價格與股票池 ──────────────────────────────────────────────────────
def _universe(market):
    try:
        with open(picks.CACHE_FILE.format(market), encoding='utf-8') as f:
            return [s['code'] for s in json.load(f)['stocks']]
    except (OSError, ValueError, KeyError):
        return []


def _latest(market):
    """(資料日期, {代碼: 收盤價})：用本地資料庫的收盤價，不另外查報價。"""
    if market == 'tw':
        with tw_board._db() as con:
            d = con.execute('SELECT MAX(date) FROM daily').fetchone()[0]
            rows = con.execute('SELECT code, close FROM daily WHERE date=?', (d,)).fetchall()
    else:
        with picks._us_db() as con:
            d = con.execute('SELECT MAX(date) FROM us_daily').fetchone()[0]
            rows = con.execute('SELECT code, close FROM us_daily WHERE date=?', (d,)).fetchall()
    return d, {c: p for c, p in rows if p}


def _closes(market, codes, dates):
    """pivot：index 日期、columns 代碼。"""
    if not codes or not dates:
        return pd.DataFrame()
    tbl, conf = ('daily', tw_board._db) if market == 'tw' else ('us_daily', picks._us_db)
    q = f"SELECT date, code, close FROM {tbl} WHERE date IN ({','.join('?' * len(dates))})"
    with conf() as con:
        df = pd.read_sql_query(q, con, params=list(dates))
    df = df[df['code'].isin(codes)]
    return df.pivot_table(index='date', columns='code', values='close')


def _trading_days(market, since):
    tbl, conf = ('daily', tw_board._db) if market == 'tw' else ('us_daily', picks._us_db)
    code = '2330' if market == 'tw' else 'AAPL'
    with conf() as con:
        return [r[0] for r in con.execute(f'SELECT date FROM {tbl} WHERE code=? AND date>=? ORDER BY date', (code, since)).fetchall()]


# ── 掃描 ──────────────────────────────────────────────────────────────
_status = {}


def _set_status(**kw):
    _status.update(kw)
    try:
        with open(STATUS_FILE + '.tmp', 'w') as f:
            json.dump(_status, f, ensure_ascii=False)
        os.replace(STATUS_FILE + '.tmp', STATUS_FILE)
    except OSError:
        pass


def read_status():
    try:
        with open(STATUS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _one(code, price):
    try:
        return financials.analyze(code, price=price)
    except Exception as e:
        if 'Too Many Requests' in str(e) or 'Rate limit' in str(e):
            raise RateLimited(str(e))
        return None, str(e)[:120]


def _row(date, market, r):
    p = {x['key']: x['score'] for x in r['pillars']}
    fv = r.get('fair') or {}
    return (date, market, r['code'], r['name'], r['sector'], r['price'], r['score'], r['quality'], r['value_score'],
            p.get('growth'), p.get('profit'), p.get('health'), p.get('quality'), r['rec']['level'],
            fv.get('value'), fv.get('upside'), fv.get('buy_below'), fv.get('confidence'), len(r['flags']))


def run_scan(market, force=False):
    date, prices = _latest(market)
    if not date:
        return
    with _db() as con:
        done = con.execute('SELECT finished FROM runs WHERE market=? AND date=?', (market, date)).fetchone()
    if done and done[0] and not force:
        return
    codes = [c for c in _universe(market) if c in prices]
    if not codes:
        return
    if not force:                                   # 中途被重啟打斷時，接著跑還沒做完的
        with _db() as con:
            have = {r[0] for r in con.execute('SELECT code FROM snaps WHERE market=? AND date=?', (market, date))}
        codes = [c for c in codes if c not in have]
    started = tw_board._now().strftime('%Y-%m-%d %H:%M')
    with _db() as con:
        con.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?)', (market, date, started, None, 0, 0))
    ok, fail = (len(_universe(market)) - len(codes)) if not force else 0, 0
    ok = max(ok, 0)
    _set_status(**{market: {'date': date, 'started': started, 'done': ok, 'total': ok + len(codes), 'running': True}})

    global _cool_until
    for c in codes:
        if not force and not _offpeak(tw_board._now()):
            _set_status(**{market: dict(_status.get(market, {}), running=False, paused='白天暫停，晚上 8:30 後繼續')})
            return
        try:
            res, err = _one(c, prices[c])
        except RateLimited:
            _cool_until = time.time() + COOLDOWN
            _set_status(**{market: dict(_status.get(market, {}), running=False, paused='Yahoo 限流，20 分鐘後繼續')})
            print(f'[fa_scan] {market}: rate limited at {c}, pausing')
            return
        if res:
            with _db() as con:
                con.execute(f'INSERT OR REPLACE INTO snaps VALUES({",".join("?" * 19)})', _row(date, market, res))
            ok += 1
        else:
            fail += 1
        if (ok + fail) % 20 == 0:
            _set_status(**{market: dict(_status.get(market, {}), done=ok + fail, fail=fail, paused=None)})
        time.sleep(PACE)
    finished = tw_board._now().strftime('%Y-%m-%d %H:%M')
    with _db() as con:
        con.execute('UPDATE runs SET finished=?, ok=?, fail=? WHERE market=? AND date=?', (finished, ok, fail, market, date))
    _set_status(**{market: {'date': date, 'started': started, 'finished': finished, 'done': ok + fail,
                            'total': len(codes), 'fail': fail, 'running': False}})
    print(f'[fa_scan] {market} {date}: {ok} ok, {fail} failed')


def _loop():
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    time.sleep(300)                    # 讓其他背景同步先跑
    while True:
        now = tw_board._now()
        if _offpeak(now) and time.time() >= _cool_until:
            for m in ('tw', 'us'):
                try:
                    run_scan(m)
                except Exception as e:
                    print(f'[fa_scan] {m}: {e}')
                if time.time() < _cool_until:
                    break
        time.sleep(600)


# ── 查詢 ──────────────────────────────────────────────────────────────
def screen(market):
    with _db() as con:
        row = con.execute('SELECT date FROM runs WHERE market=? AND finished IS NOT NULL ORDER BY date DESC LIMIT 1',
                          (market,)).fetchone()
        if not row:
            return {'date': None, 'rows': []}
        cols = ['date', 'market', 'code', 'name', 'sector', 'price', 'score', 'quality', 'value', 'growth', 'profit',
                'health', 'qual', 'rec', 'fair', 'upside', 'buy_below', 'conf', 'flags']
        rows = [dict(zip(cols, r)) for r in con.execute('SELECT * FROM snaps WHERE market=? AND date=?', (market, row[0]))]
        prev = con.execute('SELECT date FROM runs WHERE market=? AND finished IS NOT NULL AND date<? ORDER BY date DESC LIMIT 1',
                           (market, row[0])).fetchone()
        prev_rec = dict(con.execute('SELECT code, rec FROM snaps WHERE market=? AND date=?', (market, prev[0])).fetchall()) if prev else {}
    for r in rows:
        r['below_buy'] = bool(r['buy_below'] and r['price'] <= r['buy_below'])
        r['prev_rec'] = prev_rec.get(r['code'])
    return {'date': row[0], 'prev_date': prev[0] if prev else None, 'rows': rows}


def track(market):
    """建議成績單：每個建議等級在之後 N 個交易日的平均報酬、超額報酬（相對同日全部股票平均）與勝率。"""
    with _db() as con:
        snaps = pd.read_sql_query('SELECT date, code, rec, buy_below, price, score FROM snaps WHERE market=?', con, params=(market,))
    if snaps.empty:
        return {'since': None, 'days': 0, 'table': [], 'note': '還沒有掃描資料'}
    dates = sorted(snaps['date'].unique())
    tdays = _trading_days(market, dates[0])
    pos = {d: i for i, d in enumerate(tdays)}
    need = set(dates)
    pairs = {}
    for d in dates:
        if d not in pos:
            continue
        for h, _ in HORIZONS:
            j = pos[d] + h
            if j < len(tdays):
                pairs[(d, h)] = tdays[j]
                need.add(tdays[j])
    closes = _closes(market, list(snaps['code'].unique()), sorted(need))
    snaps['below'] = snaps['buy_below'].notna() & (snaps['price'] <= snaps['buy_below'])
    groups = [(k, n, snaps['rec'] == k) for k, n in LEVELS] + [('below', '低於買進價', snaps['below'])]
    q = snaps['score'].rank(pct=True)
    groups.append(('top', '綜合分數前 20%', q >= 0.8))
    table = []
    for key, name, mask in groups:
        row = {'key': key, 'name': name, 'cells': []}
        for h, hname in HORIZONS:
            rets, exc = [], []
            for d in dates:
                end = pairs.get((d, h))
                if not end or d not in closes.index or end not in closes.index:
                    continue
                day = snaps[snaps['date'] == d]
                r = (closes.loc[end] / closes.loc[d] - 1).reindex(day['code'].values)
                avg = r.mean()
                sel = r[mask[snaps['date'] == d].values].dropna()
                rets += list(sel.values)
                exc += list((sel - avg).values)
            row['cells'].append({'h': hname, 'n': len(rets),
                                 'ret': round(float(np.mean(rets)) * 100, 2) if rets else None,
                                 'excess': round(float(np.mean(exc)) * 100, 2) if exc else None,
                                 'win': round(float(np.mean(np.array(exc) > 0)) * 100, 1) if exc else None})
        table.append(row)
    days = len([d for d in tdays if d >= dates[0]])
    return {'since': dates[0], 'snapshots': len(dates), 'days': days, 'table': table,
            'horizons': [h for _, h in HORIZONS],
            'note': None if pairs else f'從 {dates[0]} 開始累積，最快 5 個交易日後出現第一筆 1 週成績'}


@bp.route('/api/fa/screen')
def api_screen():
    m = request.args.get('market', 'tw')
    if m not in ('tw', 'us'):
        return jsonify(error='market 必須是 tw 或 us'), 400
    res = screen(m)
    res['status'] = read_status().get(m)
    return jsonify(res)


@bp.route('/api/fa/track')
def api_track():
    m = request.args.get('market', 'tw')
    if m not in ('tw', 'us'):
        return jsonify(error='market 必須是 tw 或 us'), 400
    return jsonify(track(m))


def init(app, login_required):
    init_db()
    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_loop, daemon=True).start()
