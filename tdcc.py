"""集保戶股權分散表：大戶與散戶的持股變化。

集保結算所每週公布每檔股票各持股級距的人數與股數（15 級 + 合計）。常用的看法：
- 千張大戶（持有 100 萬股以上，第 15 級）的持股比例週增：大資金在買。
- 400 張以上大戶（第 12～15 級）比例：法人、主力、公司派的整體動向。
- 50 張以下散戶（第 1～8 級）比例與總股東人數：人數減少、散戶比例下降代表籌碼往大戶集中。
三大法人只看得到外資、投信、自營商；大戶持股另外涵蓋公司派、主力與大額自然人。

資料來源：
- 開放資料 opendata.tdcc.com.tw（每週一次，全部股票最新一週）：背景每 6 小時檢查，有新的一週就整批存下。
- 查詢頁 www.tdcc.com.tw/portal/zh/smWeb/qryStock（一次查一檔一週，約可查一年內）：
  個股第一次被查詢時補抓近半年；啟動時替潛力股股票池補抓前一週，讓週變化排行馬上能用。
"""
import io
import os
import re
import csv
import json
import time
import fcntl
import sqlite3
import threading

import requests
from flask import Blueprint, jsonify, request

import picks
import tw_board

bp = Blueprint('tdcc', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tdcc.db')
LOCK_FILE = os.path.join(BASE_DIR, '.tdcc.lock')
OPEN_URL = 'https://opendata.tdcc.com.tw/getOD.ashx?id=1-5'
QRY_URL = 'https://www.tdcc.com.tw/portal/zh/smWeb/qryStock'
HDRS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockLens'}
BACKFILL_WEEKS = 26
LEVELS = ['未滿 1 張', '1-5 張', '5-10 張', '10-15 張', '15-20 張', '20-30 張', '30-40 張', '40-50 張', '50-100 張',
          '100-200 張', '200-400 張', '400-600 張', '600-800 張', '800-1000 張', '1000 張以上']
_code_locks, _cl = {}, threading.Lock()


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    return con


def init_db():
    with _db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS dist(date TEXT, code TEXT, level INT, people INT, shares REAL, pct REAL,
            PRIMARY KEY(date, code, level));
        CREATE INDEX IF NOT EXISTS dist_code ON dist(code, date);
        CREATE TABLE IF NOT EXISTS done(code TEXT PRIMARY KEY, updated TEXT);
        ''')


def _n(x):
    try:
        return float(str(x).replace(',', ''))
    except ValueError:
        return 0.0


def _d(s):
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}'


# ── 抓資料 ────────────────────────────────────────────────────────────
def sync_open():
    """下載開放資料（全部股票最新一週），已經有這一週就跳過。回傳資料日期。"""
    raw = requests.get(OPEN_URL, headers=HDRS, timeout=60).content.decode('utf-8-sig', 'ignore')
    rows = list(csv.reader(io.StringIO(raw)))[1:]
    if not rows:
        return None
    date = _d(rows[0][0])
    with _db() as con:
        if con.execute('SELECT 1 FROM dist WHERE date=? LIMIT 1', (date,)).fetchone():
            return date
        con.executemany('INSERT OR REPLACE INTO dist VALUES(?,?,?,?,?,?)',
                        [(date, r[1].strip(), int(r[2]), int(_n(r[3])), _n(r[4]), _n(r[5])) for r in rows if len(r) >= 6 and r[2].isdigit()])
    print(f'[tdcc] open data {date}: {len(rows)} rows')
    return date


class _Query:
    """查詢頁要先拿表單 token，同一個 session 可以連續查。"""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HDRS)
        html = self.s.get(QRY_URL, timeout=20).text
        self.tok = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', html).group(1)
        self.uri = re.search(r'name="SYNCHRONIZER_URI" value="([^"]+)"', html).group(1)
        self.dates = re.findall(r'<option value="(\d{8})"', html)

    def get(self, code, date):
        html = self.s.post(QRY_URL, timeout=20, data={
            'SYNCHRONIZER_TOKEN': self.tok, 'SYNCHRONIZER_URI': self.uri, 'method': 'submit', 'firDate': self.dates[0],
            'scaDate': date, 'sqlMethod': 'StockNo', 'stockNo': code, 'stockName': ''}).text
        t = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', html)
        if t:
            self.tok = t.group(1)
        rows = re.findall(r'<tr>\s*<td[^>]*>(\d+)</td>\s*<td[^>]*>[^<]+</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d.]+)</td>', html)
        return [(_d(date), code, int(lv), int(_n(p)), _n(sh), _n(pc)) for lv, p, sh, pc in rows]


def backfill(code, weeks=BACKFILL_WEEKS, q=None):
    """補抓這檔股票近 N 週沒有的資料。"""
    with _cl:
        lock = _code_locks.setdefault(code, threading.Lock())
    with lock:
        q = q or _Query()
        with _db() as con:
            have = {r[0] for r in con.execute('SELECT DISTINCT date FROM dist WHERE code=?', (code,))}
        n = 0
        for d in q.dates[:weeks]:
            if _d(d) in have:
                continue
            rows = q.get(code, d)
            if rows:
                with _db() as con:
                    con.executemany('INSERT OR REPLACE INTO dist VALUES(?,?,?,?,?,?)', rows)
                n += 1
            time.sleep(0.3)
        if weeks >= BACKFILL_WEEKS:
            with _db() as con:
                con.execute('INSERT OR REPLACE INTO done VALUES(?,?)', (code, tw_board._now().strftime('%Y-%m-%d')))
        return n


def _loop():
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    time.sleep(60)
    primed = False
    while True:
        try:
            sync_open()
        except Exception as e:
            print(f'[tdcc] open data: {e}')
        if not primed:
            # 股票池補抓前一週，週變化排行才有比較基準
            try:
                q = _Query()
                prev = q.dates[1] if len(q.dates) > 1 else None
                with _db() as con:
                    have = {r[0] for r in con.execute('SELECT DISTINCT code FROM dist WHERE date=?', (_d(prev),))} if prev else set()
                codes = [c for c in _universe() if c not in have]
                for i, c in enumerate(codes):
                    try:
                        rows = q.get(c, prev)
                        if rows:
                            with _db() as con:
                                con.executemany('INSERT OR REPLACE INTO dist VALUES(?,?,?,?,?,?)', rows)
                    except Exception:
                        q = _Query()
                    time.sleep(0.4)
                primed = True
                print(f'[tdcc] primed previous week {prev} for {len(codes)} stocks')
            except Exception as e:
                print(f'[tdcc] prime: {e}')
        time.sleep(6 * 3600)


def _universe():
    try:
        with open(picks.CACHE_FILE.format('tw'), encoding='utf-8') as f:
            return [s['code'] for s in json.load(f)['stocks']]
    except (OSError, ValueError, KeyError):
        return []


# ── 分析 ──────────────────────────────────────────────────────────────
def _week(rows):
    """一週 17 級資料 → 摘要。"""
    by = {r[0]: r for r in rows}                      # level -> (level, people, shares, pct)
    pct = lambda lv: by.get(lv, (0, 0, 0, 0))[3]
    tot = by.get(17)
    if not tot:                                       # 有「差異數調整」列的週，查詢頁的合計列格式不同，改用各級距加總
        lv = [by[l] for l in range(1, 16) if l in by]
        tot = (17, sum(x[1] for x in lv), sum(x[2] for x in lv), 100.0) if lv else None
    return {'big1000': round(pct(15), 2), 'big400': round(sum(pct(l) for l in (12, 13, 14, 15)), 2),
            'retail50': round(sum(pct(l) for l in range(1, 9)), 2),
            'people': tot[1] if tot else None, 'big1000_people': by.get(15, (0, 0))[1],
            'avg_lots': round(tot[2] / tot[1] / 1000, 2) if tot and tot[1] else None}


def summary(code, ensure=True):
    with _db() as con:
        done = con.execute('SELECT updated FROM done WHERE code=?', (code,)).fetchone()
    if ensure and not done:
        try:
            backfill(code)
        except Exception as e:
            print(f'[tdcc] backfill {code}: {e}')
    with _db() as con:
        rows = con.execute('SELECT date, level, people, shares, pct FROM dist WHERE code=? ORDER BY date', (code,)).fetchall()
    if not rows:
        return None
    weeks = {}
    for d, lv, p, sh, pc in rows:
        weeks.setdefault(d, []).append((lv, p, sh, pc))
    series = [dict(date=d, **_week(v)) for d, v in sorted(weeks.items())]
    last, prev = series[-1], series[-2] if len(series) > 1 else None
    # 千張大戶比例連續增加（正數）或減少（負數）的週數
    streak, sign = 0, 0
    vals = [x['big1000'] for x in series]
    for i in range(len(vals) - 1, 0, -1):
        d = (vals[i] > vals[i - 1]) - (vals[i] < vals[i - 1])
        if d == 0 or (sign and d != sign):
            break
        sign = d
        streak += d
    chg = lambda k: round(last[k] - prev[k], 2) if prev and last[k] is not None and prev[k] is not None else None
    ppl = round((last['people'] / prev['people'] - 1) * 100, 2) if prev and prev['people'] else None
    four = series[-5] if len(series) >= 5 else None
    levels = sorted(weeks[last['date']])
    return {'code': code, 'date': last['date'], 'weeks': len(series), 'series': series[-52:], 'last': last,
            'chg': {'big1000': chg('big1000'), 'big400': chg('big400'), 'retail50': chg('retail50'), 'people_pct': ppl,
                    'big1000_4w': round(last['big1000'] - four['big1000'], 2) if four else None},
            'streak': streak, 'signal': _signal(chg('big1000'), ppl, streak),
            'levels': [{'level': LEVELS[lv - 1] if lv <= 15 else '合計', 'people': p, 'lots': round(sh / 1000), 'pct': pc}
                       for lv, p, sh, pc in levels if lv != 16]}


def _signal(d1000, ppl, streak):
    if d1000 is None:
        return None
    if d1000 > 0.3 and (ppl is None or ppl < 0):
        return {'level': 'bull', 'text': '千張大戶加碼、股東人數減少：籌碼往大戶集中'}
    if d1000 < -0.3 and (ppl is None or ppl > 0):
        return {'level': 'bear', 'text': '千張大戶減碼、股東人數增加：籌碼分散到散戶'}
    if streak >= 3:
        return {'level': 'bull', 'text': f'千張大戶連續 {streak} 週增加'}
    if streak <= -3:
        return {'level': 'bear', 'text': f'千張大戶連續 {-streak} 週減少'}
    return {'level': 'flat', 'text': '大戶持股變化不大'}


def ranking(limit=60):
    """股票池最新一週 vs 前一週：千張大戶比例變化排行。"""
    with _db() as con:
        dates = [r[0] for r in con.execute('SELECT DISTINCT date FROM dist ORDER BY date DESC LIMIT 2')]
    if len(dates) < 2:
        return {'date': dates[0] if dates else None, 'prev': None, 'up': [], 'down': []}
    uni = set(_universe())
    q = '''SELECT code, date, level, people, pct FROM dist WHERE date IN (?, ?) AND level IN (15, 12, 13, 14, 17)'''
    data = {}
    with _db() as con:
        for code, d, lv, p, pc in con.execute(q, dates):
            if code in uni:
                data.setdefault(code, {}).setdefault(d, {})[lv] = (p, pc)
    with tw_board._db() as con:
        names = dict(con.execute('SELECT code, name FROM stocks').fetchall())
        closes = {}
        latest = con.execute('SELECT MAX(date) FROM daily').fetchone()[0]
        for c, cl, chg in con.execute('SELECT code, close, chg FROM daily WHERE date=?', (latest,)):
            closes[c] = (cl, chg)
    out = []
    for code, w in data.items():
        if dates[0] not in w or dates[1] not in w:
            continue
        a, b = w[dates[0]], w[dates[1]]
        big = lambda x: x.get(15, (0, 0))[1]
        b400 = lambda x: sum(x.get(l, (0, 0))[1] for l in (12, 13, 14, 15))
        ppl = lambda x: x.get(17, (0, 0))[0]
        out.append({'code': code, 'name': names.get(code, code), 'big1000': round(big(a), 2), 'd1000': round(big(a) - big(b), 2),
                    'd400': round(b400(a) - b400(b), 2), 'people': ppl(a),
                    'd_people': round((ppl(a) / ppl(b) - 1) * 100, 2) if ppl(b) else None,
                    'close': closes.get(code, (None,))[0]})
    out.sort(key=lambda r: -r['d1000'])
    return {'date': dates[0], 'prev': dates[1], 'n': len(out), 'up': out[:limit], 'down': out[::-1][:limit]}


@bp.route('/api/tdcc/<code>')
def api_stock(code):
    code = re.sub(r'\.(TW|TWO)$', '', code.upper())
    if not re.fullmatch(r'\d{4,6}[A-Z]?', code):
        return jsonify(error='代號格式不正確'), 400
    try:
        res = summary(code)
    except Exception as e:
        return jsonify(error=f'集保資料讀取失敗：{e}'), 503
    return jsonify(res) if res else (jsonify(error='查無集保資料'), 404)


@bp.route('/api/tdcc/rank')
def api_rank():
    return jsonify(ranking())


def init(app, login_required):
    init_db()
    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_loop, daemon=True).start()
