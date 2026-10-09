# -*- coding: utf-8 -*-
"""台股看盤：全市場行情、自選股、技術分析圖、盤後選股（法人持股穿越均線）、即時選股（KDJ）。

資料來源
  - 每日行情：TWSE MI_INDEX / TPEx afterTrading/otc
  - 三大法人買賣超：TWSE T86 / TPEx insti/dailyTrade
  - 外資持股：TWSE MI_QFIIS / TPEx insti/qfii（作為外資持股的錨點）
  - 即時行情：TWSE MIS getStockInfo
  - K 線（60 分 / 日 / 週）：yfinance

法人「持股(張)」：投信、自營商沒有公開的逐日持股，因此以累計買賣超(張)表示持股變化；
外資則以最新一日的實際持股張數為錨點往回推算。金叉 / 死叉判斷不受常數偏移影響。
"""
import os
import re
import json
import time
import fcntl
import sqlite3
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from flask import Blueprint, jsonify, request, render_template

import userdata

bp = Blueprint('tw_board', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tw_market.db')
RT_FILE = os.path.join(BASE_DIR, 'tw_realtime.json')
RT_ACTIVE_FILE = os.path.join(BASE_DIR, '.tw_rt_active')
SYNC_LOCK_FILE = os.path.join(BASE_DIR, '.tw_sync.lock')
RT_LOCK_FILE = os.path.join(BASE_DIR, '.tw_rt.lock')

BACKFILL_DAYS = int(os.environ.get('TW_BACKFILL_DAYS', '500'))   # 交易日（行情 + 法人；回測 / 最佳化用）
INST_BACKFILL_DAYS = int(os.environ.get('TW_INST_BACKFILL_DAYS', '500'))   # 法人資料再往前補，持股線更完整
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
BACKUP_KEEP = 7
# 自選股、推播設定、LINE 金鑰都依帳號存在 userdata（user_data.db）
HDRS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
CODE_RE = re.compile(r'^(\d{4}|00\d{2,4}[A-Z]?)$')

INDUSTRY = {
    '01': '水泥工業', '02': '食品工業', '03': '塑膠工業', '04': '紡織纖維', '05': '電機機械',
    '06': '電器電纜', '08': '玻璃陶瓷', '09': '造紙工業', '10': '鋼鐵工業', '11': '橡膠工業',
    '12': '汽車工業', '14': '建材營造', '15': '航運業', '16': '觀光餐旅', '17': '金融保險',
    '18': '貿易百貨', '19': '綜合', '20': '其他', '21': '化學工業', '22': '生技醫療',
    '23': '油電燃氣', '24': '半導體', '25': '電腦及週邊', '26': '光電', '27': '通信網路',
    '28': '電子零組件', '29': '電子通路', '30': '資訊服務', '31': '其他電子', '32': '文化創意',
    '33': '農業科技', '34': '電子商務', '35': '綠能環保', '36': '數位雲端', '37': '運動休閒',
    '38': '居家生活', '80': '管理股票', '91': '存託憑證',
}

_status = {'phase': 'idle', 'message': '', 'updated': ''}


def _now():
    return pd.Timestamp.now(tz='Asia/Taipei')


def _num(v):
    """'1,234.5' → 1234.5；'--'、'' 等 → None"""
    if v is None:
        return None
    s = str(v).replace(',', '').strip()
    s = re.sub(r'<[^>]+>', '', s)
    try:
        return float(s)
    except ValueError:
        return None


# ── DB ────────────────────────────────────────────────────────────────
def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    return con


def _init_db():
    with _db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS stocks(code TEXT PRIMARY KEY, name TEXT, market TEXT,
            industry TEXT, kind TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, chg REAL, volume REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS inst(code TEXT, date TEXT, foreign_net REAL, trust_net REAL,
            dealer_net REAL, total_net REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS foreign_hold(code TEXT PRIMARY KEY, date TEXT, lots REAL);
        CREATE TABLE IF NOT EXISTS issued(code TEXT PRIMARY KEY, date TEXT, lots REAL);   -- 發行股數（張）
        CREATE TABLE IF NOT EXISTS concepts(label TEXT, name TEXT, code TEXT, updated TEXT,
            PRIMARY KEY(label, name, code));   -- 細產業 / 概念股 / 集團股成分股（來源：Yahoo 股市）
        CREATE TABLE IF NOT EXISTS sync_log(date TEXT, source TEXT, status TEXT, ts TEXT,
            PRIMARY KEY(date, source));
        CREATE TABLE IF NOT EXISTS index_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS market_inst(market TEXT, date TEXT, foreign_amt REAL, trust_amt REAL,
            dealer_amt REAL, total_amt REAL, PRIMARY KEY(market, date));   -- 億元
        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
        CREATE INDEX IF NOT EXISTS idx_inst_date ON inst(date);
        ''')


# ── 抓取 ──────────────────────────────────────────────────────────────
_last_req = {}


def _get_json(url, host_gap=2.5):
    host = url.split('/')[2]
    wait = _last_req.get(host, 0) + host_gap - time.time()
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(url, headers=HDRS, timeout=30)
        return r.json()
    finally:
        _last_req[host] = time.time()


def _tables(js, must_have):
    for t in js.get('tables') or []:
        fields = [str(f).strip() for f in t.get('fields') or []]
        if must_have in fields and t.get('data'):
            return fields, t['data']
    return None, None


def _fetch_twse_px(d):
    js = _get_json(f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}'
                   f'&type=ALLBUT0999&response=json')
    fields, data = _tables(js, '收盤價')
    if not data:
        return None
    ix = {f: i for i, f in enumerate(fields)}
    rows = []
    for r in data:
        code = r[ix['證券代號']].strip()
        if not CODE_RE.match(code):
            continue
        sign = '-' if '-' in r[ix['漲跌(+/-)']] else ''
        chg = _num(r[ix['漲跌價差']])
        rows.append(dict(code=code, name=r[ix['證券名稱']].strip(), open=_num(r[ix['開盤價']]),
                         high=_num(r[ix['最高價']]), low=_num(r[ix['最低價']]),
                         close=_num(r[ix['收盤價']]),
                         chg=(-chg if sign and chg is not None else chg),
                         volume=(_num(r[ix['成交股數']]) or 0) / 1000))
    return rows


def _fetch_tpex_px(d):
    js = _get_json(f'https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={d:%Y/%m/%d}'
                   f'&type=EW&response=json', host_gap=1.5)
    fields, data = _tables(js, '代號')
    if not data:
        return None
    ix = {f: i for i, f in enumerate(fields)}
    rows = []
    for r in data:
        code = r[ix['代號']].strip()
        if not CODE_RE.match(code):
            continue
        rows.append(dict(code=code, name=r[ix['名稱']].strip(), open=_num(r[ix['開盤']]),
                         high=_num(r[ix['最高']]), low=_num(r[ix['最低']]),
                         close=_num(r[ix['收盤']]), chg=_num(r[ix['漲跌']]),
                         volume=(_num(r[ix['成交股數']]) or 0) / 1000))
    return rows


def _fetch_twse_inst(d):
    js = _get_json(f'https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}'
                   f'&selectType=ALLBUT0999&response=json')
    if js.get('stat') != 'OK' or not js.get('data'):
        return None
    rows = []
    for r in js['data']:
        code = r[0].strip()
        if not CODE_RE.match(code):
            continue
        rows.append((code, (_num(r[4]) or 0) / 1000, (_num(r[10]) or 0) / 1000,
                     (_num(r[11]) or 0) / 1000, (_num(r[18]) or 0) / 1000))
    return rows


def _fetch_tpex_inst(d):
    js = _get_json(f'https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW'
                   f'&date={d:%Y/%m/%d}&response=json', host_gap=1.5)
    fields, data = _tables(js, '代號')
    if not data:
        return None
    rows = []
    for r in data:
        code = r[0].strip()
        if not CODE_RE.match(code) or len(r) < 24:
            continue
        # 4: 外資(不含外資自營商) 13: 投信 22: 自營商合計 23: 三大法人合計
        rows.append((code, (_num(r[4]) or 0) / 1000, (_num(r[13]) or 0) / 1000,
                     (_num(r[22]) or 0) / 1000, (_num(r[23]) or 0) / 1000))
    return rows


def _fetch_foreign_hold_twse(d):
    js = _get_json(f'https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?date={d:%Y%m%d}'
                   f'&selectType=ALLBUT0999&response=json')
    if js.get('stat') != 'OK':
        return []
    return [(r[0].strip(), (_num(r[5]) or 0) / 1000, (_num(r[3]) or 0) / 1000) for r in js.get('data') or []
            if CODE_RE.match(r[0].strip())]


def _fetch_foreign_hold_tpex(d):
    js = _get_json(f'https://www.tpex.org.tw/www/zh-tw/insti/qfii?type=Daily&date={d:%Y/%m/%d}'
                   f'&response=json', host_gap=1.5)
    fields, data = _tables(js, '代號')
    hk = next((f for f in fields or [] if f.startswith('僑外資及陸資持有股數')), None)
    ik = next((f for f in fields or [] if f.startswith('發行股數')), None)
    if not data or not hk or not ik:
        return []
    ix = {f: i for i, f in enumerate(fields)}
    return [(r[ix['代號']].strip(), (_num(r[ix[hk]]) or 0) / 1000, (_num(r[ix[ik]]) or 0) / 1000)
            for r in data if CODE_RE.match(r[ix['代號']].strip())]


def _fetch_twse_mkt(d):
    js = _get_json(f'https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={d:%Y%m%d}&response=json')
    if js.get('stat') != 'OK' or not js.get('data'):
        return None
    v = {r[0].strip(): (_num(r[3]) or 0) / 1e8 for r in js['data']}
    return [('TSE', v.get('外資及陸資(不含外資自營商)', 0), v.get('投信', 0),
             v.get('自營商(自行買賣)', 0) + v.get('自營商(避險)', 0), v.get('合計', 0))]


def _fetch_tpex_mkt(d):
    js = _get_json(f'https://www.tpex.org.tw/www/zh-tw/insti/summary?type=Daily&date={d:%Y/%m/%d}'
                   f'&response=json', host_gap=1.5)
    fields, data = _tables(js, '單位名稱')
    if not data:
        return None
    v = {r[0].strip().replace('\u3000', '').rstrip('*'): (_num(r[3]) or 0) / 1e8 for r in data}
    return [('OTC', v.get('外資及陸資(不含自營商)', 0), v.get('投信', 0), v.get('自營商合計', 0),
             v.get('三大法人合計', 0))]


def _fetch_index_month(code, month):
    """指數某個月的每日開高低收（官方資料）。month = Timestamp（該月任一天）。"""
    if code == 'TAIEX':
        js = _get_json(f'https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={month:%Y%m}01&response=json')
        rows = (js.get('data') or []) if js.get('stat') == 'OK' else []
        out = []
        for r in rows:
            y, m, dd = r[0].split('/')
            out.append((f'{int(y) + 1911}-{m}-{dd}', *(_num(x) for x in r[1:5])))
        return out
    js = _get_json(f'https://www.tpex.org.tw/www/zh-tw/indexInfo/inx?date={month:%Y/%m}/01&response=json',
                   host_gap=1.5)
    fields, data = _tables(js, '日期')
    return [(r[0].replace('/', '-'), *(_num(x) for x in r[1:5])) for r in data or []]


INDEX_META = {
    'TAIEX': dict(name='加權指數', market='TSE', yf='^TWII', mis='t00', alias='大盤 加權 指數 TAIEX 台股'),
    'TPEX': dict(name='櫃買指數', market='OTC', yf=None, mis='o00', alias='大盤 櫃買 櫃檯 指數 TPEX OTC'),
}
INDEX_HISTORY_MONTHS = 72


def _sync_index_history():
    """指數月資料：過去的月份抓一次，當月每次同步都更新。"""
    this_month = _now().strftime('%Y-%m')
    for code in INDEX_META:
        src = f'idx_{code}'
        with _db() as con:
            done = {r[0] for r in con.execute('SELECT date FROM sync_log WHERE source=?', (src,))}
        for i in range(INDEX_HISTORY_MONTHS):
            month = (_now().tz_localize(None).to_period('M') - i).to_timestamp()
            key = month.strftime('%Y-%m')
            if key in done and key != this_month:
                continue
            try:
                rows = _fetch_index_month(code, month)
            except Exception as e:
                print(f'[tw_board] index {code} {key}: {e}')
                continue
            with _db() as con:
                con.executemany('INSERT OR REPLACE INTO index_daily VALUES(?,?,?,?,?,?)',
                                [(code, *r) for r in rows if r[4] is not None])
                if rows or key < this_month:
                    con.execute('INSERT OR REPLACE INTO sync_log VALUES(?,?,?,?)',
                                (key, src, 'ok' if rows else 'nodata', _now().strftime('%Y-%m-%d %H:%M')))


# ── 同步 ──────────────────────────────────────────────────────────────
SOURCES = {
    'twse_px': _fetch_twse_px, 'tpex_px': _fetch_tpex_px,
    'twse_inst': _fetch_twse_inst, 'tpex_inst': _fetch_tpex_inst,
    'twse_mkt': _fetch_twse_mkt, 'tpex_mkt': _fetch_tpex_mkt,
}


def _done_sources(day):
    with _db() as con:
        return {s for s, in con.execute(
            'SELECT source FROM sync_log WHERE date=?', (day,))}


def _sync_source(src, d, today):
    day = d.strftime('%Y-%m-%d')
    try:
        rows = SOURCES[src](d)
    except Exception as e:
        print(f'[tw_board] {src} {day}: {e}')
        return False
    ts = _now().strftime('%Y-%m-%d %H:%M')
    with _db() as con:
        if rows:
            if src.endswith('_px'):
                market = 'TSE' if src.startswith('twse') else 'OTC'
                con.executemany(
                    'INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?)',
                    [(r['code'], day, r['open'], r['high'], r['low'], r['close'], r['chg'],
                      r['volume']) for r in rows if r['close'] is not None])
                con.executemany(
                    'INSERT INTO stocks(code,name,market,industry,kind,updated) '
                    'VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET '
                    'name=excluded.name, market=excluded.market, updated=excluded.updated '
                    'WHERE excluded.updated >= stocks.updated',
                    [(r['code'], r['name'], market,
                      'ETF' if r['code'].startswith('00') else '',
                      'etf' if r['code'].startswith('00') else 'stock', day) for r in rows])
            elif src.endswith('_mkt'):
                con.executemany('INSERT OR REPLACE INTO market_inst VALUES(?,?,?,?,?,?)',
                                [(m, day, a, b, e, f) for m, a, b, e, f in rows])
            else:
                con.executemany('INSERT OR REPLACE INTO inst VALUES(?,?,?,?,?,?)',
                                [(c, day, a, b, e, f) for c, a, b, e, f in rows])
            con.execute('INSERT OR REPLACE INTO sync_log VALUES(?,?,?,?)', (day, src, 'ok', ts))
        elif d.date() < today:
            # 過去日期無資料 = 休市，記下來避免重抓；當日資料可能尚未公布則稍後重試
            con.execute('INSERT OR REPLACE INTO sync_log VALUES(?,?,?,?)', (day, src, 'nodata', ts))
    return bool(rows)


def _sync_day(d, today, sources=None):
    """TWSE 與 TPEx 是不同主機，兩邊平行抓。"""
    done = _done_sources(d.strftime('%Y-%m-%d'))
    want = set(sources or SOURCES)

    def run(srcs):
        for src in srcs:
            if src in want and src not in done:
                _sync_source(src, d, today)

    th = threading.Thread(target=run, args=(['tpex_px', 'tpex_inst', 'tpex_mkt'],))
    th.start()
    run(['twse_px', 'twse_inst', 'twse_mkt'])
    th.join()


def _sync_meta():
    """產業別；一天更新一次。"""
    try:
        with _db() as con:
            last = con.execute("SELECT MAX(updated) FROM stocks WHERE industry NOT IN ('','ETF')"
                               ).fetchone()[0]
        mp = {}
        for url, ck, ik in [
            ('https://openapi.twse.com.tw/v1/opendata/t187ap03_L', '公司代號', '產業別'),
            ('https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O', 'SecuritiesCompanyCode',
             'SecuritiesIndustryCode'),
        ]:
            for r in _get_json(url, host_gap=1.0):
                mp[str(r.get(ck, '')).strip()] = INDUSTRY.get(str(r.get(ik, '')).strip(), '其他')
        with _db() as con:
            con.executemany('UPDATE stocks SET industry=? WHERE code=?',
                            [(v, k) for k, v in mp.items()])
        return last
    except Exception as e:
        print(f'[tw_board] meta: {e}')


def _sync_foreign_hold():
    """外資實際持股（最近一個有資料的交易日），作為外資持股線的錨點。"""
    for market, src, fn in (('TSE', 'twse_inst', _fetch_foreign_hold_twse),
                            ('OTC', 'tpex_inst', _fetch_foreign_hold_tpex)):
        with _db() as con:
            days = [r[0] for r in con.execute(
                "SELECT date FROM sync_log WHERE source=? AND status='ok' "
                "ORDER BY date DESC LIMIT 3", (src,))]
            f_have = con.execute('SELECT MAX(f.date) FROM foreign_hold f JOIN stocks s ON s.code=f.code '
                                 'WHERE s.market=?', (market,)).fetchone()[0]
            i_have = con.execute('SELECT MAX(i.date) FROM issued i JOIN stocks s ON s.code=i.code '
                                 'WHERE s.market=?', (market,)).fetchone()[0]
            have = min(f_have, i_have) if f_have and i_have else None   # 兩者都有才算已更新
        for day in days:
            if have and have >= day:
                break
            try:
                rows = fn(pd.Timestamp(day))
            except Exception as e:
                print(f'[tw_board] foreign_hold {market} {day}: {e}')
                continue
            if rows:
                with _db() as con:
                    con.executemany('INSERT OR REPLACE INTO foreign_hold VALUES(?,?,?)',
                                    [(c, day, v) for c, v, _ in rows])
                    con.executemany('INSERT OR REPLACE INTO issued VALUES(?,?,?)',
                                    [(c, day, n) for c, _, n in rows if n])
                break


CONCEPT_LABELS = {'sub': '電子產業', 'concept': '概念股', 'group': '集團股'}


def _sync_concepts(force=False):
    """細產業、概念股、集團股的成分股：每週從 Yahoo 股市更新一次（官方沒有概念股分類）。"""
    with _db() as con:
        last = con.execute('SELECT MAX(updated) FROM concepts').fetchone()[0]
    if not force and last and last >= (_now() - pd.Timedelta(days=7)).strftime('%Y-%m-%d'):
        return
    import html as _html
    import urllib.parse as up
    hdr = dict(HDRS)
    try:
        page = requests.get('https://tw.stock.yahoo.com/class', headers=hdr, timeout=30).text
    except Exception as e:
        print(f'[tw_board] concepts index: {e}')
        return
    cats = []
    for u in re.findall(r'href="(/class-quote\?category=[^"]+)"', page):
        q = dict(up.parse_qsl(_html.unescape(u).split('?', 1)[1]))
        if q.get('categoryLabel') in CONCEPT_LABELS.values() and (q['categoryLabel'], q['category']) not in cats:
            cats.append((q['categoryLabel'], q['category']))
    today = _now().strftime('%Y-%m-%d')
    done = 0
    for label, name in cats:
        codes, offset = set(), 0
        try:
            while offset is not None and offset < 1000:
                url = ('https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.getClassQuotes;'
                       f'categoryName={up.quote(name, safe="")};offset={offset}')
                js = _get_json(url, host_gap=0.8)
                codes |= {x.get('systexId') for x in js.get('list') or [] if CODE_RE.match(str(x.get('systexId', '')))}
                nxt = (js.get('pagination') or {}).get('nextOffset')
                offset = int(nxt) if nxt not in (None, '') else None
        except Exception as e:
            print(f'[tw_board] concept {name}: {e}')
            continue
        if codes:
            with _db() as con:
                con.execute('DELETE FROM concepts WHERE label=? AND name=?', (label, name))
                con.executemany('INSERT INTO concepts VALUES(?,?,?,?)', [(label, name, c, today) for c in codes])
            done += 1
    print(f'[tw_board] concepts updated: {done}/{len(cats)}')


def _trading_days_loaded(source='twse_px'):
    with _db() as con:
        return con.execute("SELECT COUNT(*) FROM sync_log WHERE source=? AND status='ok'",
                           (source,)).fetchone()[0]


def _backup_db():
    """每天備份一次資料庫（gzip），保留最近 BACKUP_KEEP 份。"""
    import gzip
    import shutil
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = os.path.join(BACKUP_DIR, f"tw_market-{_now():%Y%m%d}.db.gz")
    if os.path.exists(name):
        return
    tmp = os.path.join(BACKUP_DIR, '.backup.tmp')
    src = _db()
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)   # SQLite 線上備份，不必停機
    finally:
        dst.close()
        src.close()
    with open(tmp, 'rb') as fi, gzip.open(name + '.part', 'wb', compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.replace(name + '.part', name)
    os.remove(tmp)
    for old in sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('tw_market-'))[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))


def run_sync():
    today = _now().date()
    _status.update(phase='sync', message='更新今日資料')
    now = _now()
    if now.weekday() < 5 and now.hour >= 14:
        _sync_day(pd.Timestamp(today), today)
    _status.update(phase='sync', message='更新大盤指數')
    _sync_index_history()
    # 回補歷史（由新到舊）
    span = int(BACKFILL_DAYS * 7 / 5) + 30
    for i in range(1, span):
        d = pd.Timestamp(today - dt.timedelta(days=i))
        if d.weekday() >= 5:
            continue
        day = d.strftime('%Y-%m-%d')
        if len(_done_sources(day)) >= len(SOURCES):
            continue
        n = _trading_days_loaded()
        if n >= BACKFILL_DAYS:
            break
        _status.update(phase='backfill', message=f'回補歷史 {day}（已載入 {n} 個交易日）')
        _write_status()
        _sync_day(d, today)
    # 法人買賣超再往前補（只抓法人，持股估計線更長）
    span = int(INST_BACKFILL_DAYS * 7 / 5) + 30
    for i in range(1, span):
        if min(_trading_days_loaded('twse_inst'), _trading_days_loaded('twse_mkt')) >= INST_BACKFILL_DAYS:
            break
        d = pd.Timestamp(today - dt.timedelta(days=i))
        if d.weekday() >= 5:
            continue
        done = _done_sources(d.strftime('%Y-%m-%d'))
        if {'twse_inst', 'tpex_inst', 'twse_mkt', 'tpex_mkt'} <= done:
            continue
        _status.update(phase='backfill', message=f"回補法人資料 {d:%Y-%m-%d}（{_trading_days_loaded('twse_inst')} 日）")
        _write_status()
        _sync_day(d, today, ['twse_inst', 'tpex_inst', 'twse_mkt', 'tpex_mkt'])
    _sync_foreign_hold()
    try:
        _sync_concepts()
    except Exception as e:
        print(f'[tw_board] concepts: {e}')
    with _db() as con:
        need_meta = con.execute("SELECT COUNT(*) FROM stocks WHERE industry=''").fetchone()[0]
    if need_meta or _status.get('meta_day') != str(today):
        _sync_meta()
        _status['meta_day'] = str(today)
    try:
        get_scorecard()
    except Exception as e:
        print(f'[tw_board] scorecard: {e}')
    if _now().hour >= 18 or _now().weekday() >= 5:
        try:
            _backup_db()
        except Exception as e:
            print(f'[tw_board] backup: {e}')
    _status.update(phase='idle', message=f'已載入 {_trading_days_loaded()} 個交易日',
                   updated=_now().strftime('%Y-%m-%d %H:%M'))


def _sync_loop():
    while True:
        lock = open(SYNC_LOCK_FILE, 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            time.sleep(60)
            continue
        while True:   # 取得鎖的 worker 負責同步
            try:
                run_sync()
                _write_status()
            except Exception as e:
                print(f'[tw_board] sync loop: {e}')
            time.sleep(600)


STATUS_FILE = os.path.join(BASE_DIR, '.tw_sync_status.json')


def _write_status():
    try:
        with open(STATUS_FILE + '.tmp', 'w') as f:
            json.dump(_status, f, ensure_ascii=False)
        os.replace(STATUS_FILE + '.tmp', STATUS_FILE)
    except OSError:
        pass


# ── 即時行情 ──────────────────────────────────────────────────────────
def _market_open(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1335


def _touch_active():
    try:
        with open(RT_ACTIVE_FILE, 'a'):
            os.utime(RT_ACTIVE_FILE, None)
    except OSError:
        pass


def _rt_wanted():
    try:
        return time.time() - os.path.getmtime(RT_ACTIVE_FILE) < 180
    except OSError:
        return False


def _poll_realtime_once(prev):
    with _db() as con:
        rows = con.execute('SELECT code, market FROM stocks').fetchall()
    keys = ['tse_t00.tw', 'otc_o00.tw'] + [f"{'tse' if m == 'TSE' else 'otc'}_{c}.tw" for c, m in rows]
    quotes = dict(prev.get('quotes', {}))

    def fetch(chunk):
        """失敗（逾時、回傳非 JSON）時隔 2 秒重試一次；仍失敗回傳 None，這批沿用上一筆報價。"""
        url = ('https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch='
               + '|'.join(chunk))
        for attempt in (1, 2):
            try:
                return requests.get(url, headers=HDRS, timeout=12).json().get('msgArray', [])
            except Exception as e:
                if attempt == 2:
                    print(f'[tw_board] mis: {e}')
                    return None
                time.sleep(2)

    chunks = [keys[i:i + 90] for i in range(0, len(keys), 90)]
    failed = 0
    with ThreadPoolExecutor(4) as ex:
        for msgs in ex.map(fetch, chunks):
            if msgs is None:
                failed += 1
                continue
            for m in msgs:
                code = m.get('c')
                if not code:
                    continue
                z = _num(m.get('z'))
                if z is None:   # 這一刻沒有成交，沿用上一筆或最佳買價
                    old = quotes.get(code)
                    z = (old or {}).get('price') or _num((m.get('b') or '').split('_')[0])
                quotes[code] = dict(price=z, prev=_num(m.get('y')), open=_num(m.get('o')),
                                    high=_num(m.get('h')), low=_num(m.get('l')),
                                    volume=_num(m.get('v')), date=m.get('d'), time=m.get('t'))
    if failed == len(chunks) and prev.get('ts'):
        # 整輪都失敗：不更新時間戳，畫面上看得出報價停在上一次
        return prev
    # 快照日期用證交所回報的成交日期（休市日 MIS 仍會回傳上一個交易日的報價，不能當成今天的即時資料）
    from collections import Counter
    trade_dates = Counter(q.get('date') for q in quotes.values() if q.get('date'))
    snap_date = trade_dates.most_common(1)[0][0] if trade_dates else _now().strftime('%Y%m%d')
    snap = dict(ts=_now().strftime('%Y-%m-%d %H:%M:%S'), date=snap_date,
                quotes=quotes, failed_chunks=failed, total_chunks=len(chunks))
    with open(RT_FILE + '.tmp', 'w') as f:
        json.dump(snap, f)
    os.replace(RT_FILE + '.tmp', RT_FILE)
    return snap


def _rt_loop():
    while True:
        lock = open(RT_LOCK_FILE, 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            time.sleep(60)
            continue
        snap = _read_rt() or {}
        while True:
            t0 = time.time()
            try:
                alerts_on = any(c.get('enabled') for _, c in userdata.users_with('tw_alerts'))
                if (_rt_wanted() or alerts_on) and _market_open():
                    snap = _poll_realtime_once(snap if snap.get('date') == _now().strftime('%Y%m%d')
                                               else {})
                    if alerts_on:
                        _check_alerts()
            except Exception as e:
                print(f'[tw_board] rt loop: {e}')
            time.sleep(max(5, 20 - (time.time() - t0)))


_rt_cache = {'mtime': 0, 'data': None}


def _read_rt():
    try:
        mt = os.path.getmtime(RT_FILE)
    except OSError:
        return None
    if mt != _rt_cache['mtime']:
        try:
            with open(RT_FILE) as f:
                _rt_cache['data'] = json.load(f)
            _rt_cache['mtime'] = mt
        except (OSError, ValueError):
            return _rt_cache['data']
    return _rt_cache['data']


def _rt_today():
    """當日盤中即時報價（非當日的快照不回傳）。"""
    snap = _read_rt()
    if snap and snap.get('date') == _now().strftime('%Y%m%d'):
        return snap
    return None


# ── 計算 ──────────────────────────────────────────────────────────────
def calc_kd_frame(high, low, close, n=9):
    """多檔同時計算 KD（列 = 日期、欄 = 代碼），初始值 50，K = 2/3 K' + 1/3 RSV。"""
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    rng = (hhv - llv).replace(0, np.nan)
    rsv = ((close - llv) / rng * 100).fillna(50).values
    k = np.full(rsv.shape, np.nan)
    d = np.full(rsv.shape, np.nan)
    pk = np.full(rsv.shape[1], 50.0)
    pd_ = np.full(rsv.shape[1], 50.0)
    for i in range(rsv.shape[0]):
        pk = pk * 2 / 3 + rsv[i] / 3
        pd_ = pd_ * 2 / 3 + pk / 3
        k[i], d[i] = pk, pd_
    return (pd.DataFrame(k, index=close.index, columns=close.columns),
            pd.DataFrame(d, index=close.index, columns=close.columns))


_frames_cache = {}
_frames_lock = threading.Lock()


def _data_version():
    with _db() as con:
        return con.execute('SELECT MAX(ts), COUNT(*) FROM sync_log').fetchone()


def _load_frames(n_days, asof=None):
    """選股用的寬表（日期 × 代碼）。資料沒更新前重複使用，回傳複本避免被呼叫端改到。"""
    key = (n_days, asof, _data_version())
    with _frames_lock:
        hit = _frames_cache.get(key)
        if hit is None:
            hit = _load_frames_db(n_days, asof)
            if len(_frames_cache) > 6:
                _frames_cache.clear()
            _frames_cache[key] = hit
    if hit is None:
        return None
    return {k: (v.copy() if isinstance(v, pd.DataFrame) else v) for k, v in hit.items()}


def _load_frames_db(n_days, asof=None):
    with _db() as con:
        q = "SELECT date FROM sync_log WHERE source='twse_px' AND status='ok'"
        args = []
        if asof:
            q += ' AND date<=?'
            args.append(asof)
        dates = [r[0] for r in con.execute(q + ' ORDER BY date DESC LIMIT ?', args + [n_days])]
        if not dates:
            return None
        dates.sort()
        px = pd.read_sql_query('SELECT * FROM daily WHERE date>=? AND date<=?', con,
                               params=(dates[0], dates[-1]))
        inst = pd.read_sql_query('SELECT * FROM inst WHERE date>=? AND date<=?', con,
                                 params=(dates[0], dates[-1]))
        meta = pd.read_sql_query('SELECT * FROM stocks', con).set_index('code')
        issued = dict(con.execute('SELECT code, lots FROM issued'))
        fhold = dict(con.execute('SELECT code, lots FROM foreign_hold'))
    idx = pd.Index(dates)

    def piv(df, col, fill=None):
        p = df.pivot(index='date', columns='code', values=col).reindex(idx)
        return p.fillna(fill) if fill is not None else p

    close = piv(px, 'close').ffill()
    f = dict(close=close, high=piv(px, 'high').ffill(), low=piv(px, 'low').ffill(),
             volume=piv(px, 'volume', 0), chg=piv(px, 'chg'), dates=dates, meta=meta)
    for col in ('foreign_net', 'trust_net', 'dealer_net', 'total_net'):
        f[col] = piv(inst, col, 0).reindex(columns=close.columns, fill_value=0)
    f['issued'] = pd.Series(issued, dtype=float).reindex(close.columns)          # 發行股數（張）
    f['foreign_ratio'] = (pd.Series(fhold, dtype=float).reindex(close.columns) / f['issued'] * 100)
    return f


INVESTORS = {'trust': ('trust_net', '投信'), 'foreign': ('foreign_net', '外資'),
             'dealer': ('dealer_net', '自營商'), 'total': ('total_net', '法人')}


def _cross_up(a, b):
    return (a.shift(1) <= b.shift(1)) & (a > b)


def _cross_dn(a, b):
    return (a.shift(1) >= b.shift(1)) & (a < b)


def _row_base(code, f, meta):
    m = meta.loc[code] if code in meta.index else None
    c = f['close'][code].iloc[-1]
    chg = f['chg'][code].iloc[-1]
    prev = c - chg if pd.notna(chg) else None
    return dict(code=code, name=(m['name'] if m is not None else code),
                market=(m['market'] if m is not None else ''),
                industry=(m['industry'] if m is not None else ''),
                close=round(float(c), 2) if pd.notna(c) else None,
                chg=round(float(chg), 2) if pd.notna(chg) else None,
                chg_pct=round(float(chg / prev * 100), 2) if prev else None,
                volume=round(float(f['volume'][code].iloc[-1])))


def screen_after_hours(rules, ma_n=5, within=1, kd_n=9, min_volume=0, include_etf=False,
                       asof=None):
    """rules: [{'investor': 'trust', 'side': 'buy'|'sell'}, ...]；多條規則為 OR。"""
    f = _load_frames(max(80, ma_n + within + kd_n + 40), asof)
    if f is None or len(f['dates']) < ma_n + 2:
        return None
    k, d = calc_kd_frame(f['high'], f['low'], f['close'], kd_n)   # 只用來在結果中顯示 K / D
    k_last, d_last = k.iloc[-1], d.iloc[-1]
    meta = f['meta']
    vol_ok = f['volume'].iloc[-1] >= min_volume
    out = {}
    for rule in rules:
        col, label = INVESTORS[rule['investor']]
        hold = f[col].cumsum()
        ma = hold.rolling(ma_n).mean()
        # 持股線穿越 N 日均線（within 日內發生過，且目前仍在均線同一側）
        if rule['side'] == 'buy':
            hit = _cross_up(hold, ma).iloc[-within:].any() & (hold.iloc[-1] > ma.iloc[-1])
        else:
            hit = _cross_dn(hold, ma).iloc[-within:].any() & (hold.iloc[-1] < ma.iloc[-1])
        hit &= vol_ok
        for code in hit[hit].index:
            if not include_etf and code.startswith('00'):
                continue
            row = out.get(code) or _row_base(code, f, meta)
            row.setdefault('signals', []).append(
                dict(investor=rule['investor'], label=label, side=rule['side'],
                     net=round(float(f[col][code].iloc[-1]), 1)))
            row['k'] = round(float(k_last[code]), 1)
            row['d'] = round(float(d_last[code]), 1)
            out[code] = row
    return dict(date=f['dates'][-1], days=len(f['dates']), results=list(out.values()))


def backtest_after_hours(rules, ma_list=(3, 5), within=1, kd_n=9, min_volume=0,
                         include_etf=False, horizons=(5, 10, 20)):
    """把盤後選股條件套用到歷史上每一天，統計訊號出現後 N 日的報酬。

    買進訊號的「勝率」= 之後上漲的比例；賣出訊號的「勝率」= 之後下跌的比例。
    報酬以收盤價計算，未還原除權息。
    """
    f = _load_frames(BACKFILL_DAYS + 10)
    if f is None or len(f['dates']) < 60:
        return None
    close, vol = f['close'], f['volume']
    cols = [c for c in close.columns if include_etf or not c.startswith('00')]
    close, vol = close[cols], vol[cols]
    warm = max(30, max(ma_list) + 2)              # 均線暖機期不計
    valid = pd.Series(np.arange(len(close)) >= warm, index=close.index)

    def recent(ev):
        return ev.rolling(within, min_periods=1).max().astype(bool) if within > 1 else ev

    fwd = {h: close.shift(-h) / close - 1 for h in horizons}
    vol_ok = vol >= min_volume
    base = {h: float(np.nanmean(fwd[h].values[warm:])) * 100 for h in horizons}
    out = []
    for ma_n in ma_list:
        for rule in rules:
            col, label = INVESTORS[rule['investor']]
            hold = f[col][cols].cumsum()
            ma = hold.rolling(ma_n).mean()
            if rule['side'] == 'buy':
                hit = recent(_cross_up(hold, ma)) & (hold > ma)
            else:
                hit = recent(_cross_dn(hold, ma)) & (hold < ma)
            hit = hit & vol_ok & valid.values[:, None]
            row = dict(ma_n=ma_n, investor=rule['investor'], label=label, side=rule['side'],
                       signals=int(hit.values.sum()), stats={})
            for h in horizons:
                r = fwd[h].values[hit.values]
                r = r[~np.isnan(r)]
                if len(r):
                    win = (r > 0) if rule['side'] == 'buy' else (r < 0)
                    row['stats'][h] = dict(n=int(len(r)), avg=round(float(r.mean()) * 100, 2),
                                           median=round(float(np.median(r)) * 100, 2),
                                           win=round(float(win.mean()) * 100, 1))
            out.append(row)
    return dict(start=f['dates'][warm], end=f['dates'][-1], days=len(f['dates']) - warm,
                horizons=list(horizons), baseline={h: round(v, 2) for h, v in base.items()},
                results=out)


def inst_ranking(investor='total', side='buy', days=1, by='lots', limit=50, include_etf=False,
                 market=''):
    """法人買賣超排行：最近 days 個交易日累計，依張數或金額排序。

    金額（億元）= 每日買賣超張數 × 當日收盤 × 1000，逐日加總；連續天數 = 到最新一日為止，
    同方向（買超 / 賣超）連續了幾天。
    """
    col, label = INVESTORS[investor]
    f = _load_frames(max(days, 30) + 5)
    if f is None or len(f['dates']) < days:
        return None
    net = f[col]
    close = f['close'].reindex(columns=net.columns)
    vol = f['volume'].reindex(columns=net.columns)
    window = net.iloc[-days:]
    lots = window.sum()
    amount = (window * close.iloc[-days:] * 1000).sum() / 1e8
    vol_sum = vol.iloc[-days:].sum()
    sign = 1 if side == 'buy' else -1
    # 連續買（賣）超天數
    same = (np.sign(net.values) == sign)
    streak = np.zeros(net.shape[1], dtype=int)
    alive = np.ones(net.shape[1], dtype=bool)
    for i in range(len(net) - 1, -1, -1):
        alive &= same[i]
        if not alive.any():
            break
        streak += alive
    df = pd.DataFrame({'lots': lots, 'amount': amount, 'vol': vol_sum,
                       'streak': pd.Series(streak, index=net.columns)})
    meta = f['meta']
    df = df[df.index.isin(meta.index)]
    if not include_etf:
        df = df[~df.index.str.startswith('00')]
    if market:
        df = df[meta.loc[df.index, 'market'] == market]
    key = 'amount' if by == 'amount' else 'lots'
    df = df[df[key] * sign > 0].sort_values(key, ascending=(sign < 0)).head(limit)
    out = []
    for rank, (code, r) in enumerate(df.iterrows(), 1):
        row = _row_base(code, f, meta)
        row.update(rank=rank, net=round(float(r['lots'])), amount=round(float(r['amount']), 2),
                   ratio=round(float(r['lots'] / r['vol'] * 100), 1) if r['vol'] else None,
                   streak=int(r['streak']))
        out.append(row)
    return dict(date=f['dates'][-1], start=f['dates'][-days], days=days, investor=investor,
                label=label, side=side, by=key, results=out)


# ── 法人持股比例 ─────────────────────────────────────────────────────
# 持股比例的變化（百分點）＝ N 日內買賣超張數合計 ÷ 發行張數 × 100。
# 與「目前實際持股多少」無關，所以投信 / 自營商也能精確計算。
def _ratio_change(f, col, days):
    return f[col].rolling(days, min_periods=days).sum().div(f['issued'], axis=1) * 100


def screen_ratio(rules, days=5, threshold=1.0, min_volume=0, include_etf=False, asof=None):
    """最近 days 天，法人持股比例增加（BUY）/ 減少（SELL）至少 threshold 個百分點。多條規則為 OR。"""
    f = _load_frames(max(days + 5, 40), asof)
    if f is None or len(f['dates']) < days:
        return None
    meta = f['meta']
    vol_ok = f['volume'].iloc[-1] >= min_volume
    out = {}
    for rule in rules:
        col, label = INVESTORS[rule['investor']]
        chg = _ratio_change(f, col, days).iloc[-1]
        hit = (chg >= threshold) if rule['side'] == 'buy' else (chg <= -threshold)
        hit &= vol_ok
        for code in hit[hit.fillna(False)].index:
            if not include_etf and code.startswith('00'):
                continue
            row = out.get(code) or _row_base(code, f, meta)
            row.setdefault('signals', []).append(dict(investor=rule['investor'], label=label, side=rule['side'],
                                                      ratio_chg=round(float(chg[code]), 2)))
            row['ratio_chg'] = max((s['ratio_chg'] for s in row['signals']), key=abs)
            fr = f['foreign_ratio'].get(code)
            row['foreign_ratio'] = round(float(fr), 2) if pd.notna(fr) else None
            out[code] = row
    return dict(date=f['dates'][-1], start=f['dates'][-days], days=days, threshold=threshold,
                results=list(out.values()))


def optimize_ratio(rules, days_list=(3, 5, 10, 20), th_list=(0.5, 1, 2, 3), horizon=10,
                   objective='excess', min_signals=30, train_frac=0.7, min_volume=0, include_etf=False):
    """參數最佳化：把每組 (天數, 門檻) 套用到歷史上，統計訊號出現後 horizon 日的報酬。

    - 訊號只算「條件剛成立的那一天」，避免同一波重複計算。
    - 前 train_frac 的期間用來挑參數（訓練），之後的期間只用來驗證（測試）；
      測試期的表現才代表參數在沒看過的資料上是否仍然有效，避免過度擬合。
    - 超額報酬 = 訊號股的報酬 − 同一天全市場平均報酬，排除大盤漲跌的影響；
      勝過大盤 = 超額報酬 > 0 的比例（SELL 則看 < 0）。預設以超額報酬挑參數。
    - 報酬以收盤價計算，未還原除權息；BUY 勝率 = 之後上漲比例，SELL 勝率 = 之後下跌比例。
    """
    f = _load_frames(BACKFILL_DAYS + 10)
    if f is None or len(f['dates']) < max(days_list) + horizon + 40:
        return None
    cols = [c for c in f['close'].columns if (include_etf or not c.startswith('00')) and pd.notna(f['issued'].get(c))]
    close, vol = f['close'][cols], f['volume'][cols]
    fwd = (close.shift(-horizon) / close - 1).values
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)          # 最後 horizon 天沒有未來報酬
        excess = fwd - np.nanmean(fwd, axis=1, keepdims=True)    # 相對同日全市場
    n = len(close)
    split = int(n * train_frac)
    period = np.zeros(n, dtype=int)            # 0 = 訓練期、1 = 測試期
    period[split:] = 1
    warm = max(days_list) + 1
    vol_ok = (vol >= min_volume).values
    base = {}
    for p in (0, 1):
        rows = (np.arange(n) >= warm) & (period == p)
        base[p] = float(np.nanmean(fwd[rows])) * 100
    sub = {k: f[k][cols] if isinstance(f[k], pd.DataFrame) else f[k] for k in ('trust_net', 'foreign_net', 'dealer_net', 'total_net')}
    sub['issued'] = f['issued'][cols]

    def stats(m, side):
        r, x = fwd[m], excess[m]
        ok = ~np.isnan(r)
        r, x = r[ok], x[ok]
        if not len(r):
            return None
        sgn = 1 if side == 'buy' else -1
        return dict(n=int(len(r)), avg=round(float(r.mean()) * 100, 2), median=round(float(np.median(r)) * 100, 2),
                    win=round(float((r * sgn > 0).mean()) * 100, 1),
                    excess=round(float(x.mean()) * 100, 2), beat=round(float((x * sgn > 0).mean()) * 100, 1))

    results = []
    for rule in rules:
        col, label = INVESTORS[rule['investor']]
        grid = []
        for days in days_list:
            chg = _ratio_change(sub, col, days).values
            for th in th_list:
                cond = (chg >= th) if rule['side'] == 'buy' else (chg <= -th)
                trig = cond & ~np.vstack([np.zeros((1, cond.shape[1]), bool), cond[:-1]])   # 剛成立
                trig &= vol_ok
                trig[:warm] = False
                cell = dict(days=days, threshold=th)
                for p, name in ((0, 'train'), (1, 'test')):
                    m = trig & (period == p)[:, None]
                    cell[name] = stats(m, rule['side'])
                grid.append(cell)
        # 依訓練期挑最佳（訊號數不足的不列入），測試期只看不挑
        def score(c):
            t = c['train']
            if not t or t['n'] < min_signals:
                return -1e9
            v = t[objective]
            # 報酬類：BUY 越高越好、SELL 越低（跌越多）越好；勝率類兩邊都是越高越好
            return -v if rule['side'] == 'sell' and objective in ('avg', 'excess') else v
        ranked = sorted(grid, key=score, reverse=True)
        results.append(dict(investor=rule['investor'], label=label, side=rule['side'],
                            best=ranked[0] if score(ranked[0]) > -1e9 else None,
                            top=[c for c in ranked[:5] if score(c) > -1e9], grid=grid))
    return dict(start=f['dates'][warm], split=f['dates'][split], end=f['dates'][-1], horizon=horizon,
                objective=objective, min_signals=min_signals, baseline={'train': round(base[0], 2), 'test': round(base[1], 2)},
                days_list=list(days_list), th_list=list(th_list), results=results)


def _group_map(kind, meta):
    """代碼 → [群組]。kind = industry（官方產業）/ sub（細產業）/ concept（概念股）/ group（集團股）。"""
    if kind == 'industry':
        ind = meta['industry'].dropna()
        ind = ind[~ind.isin(['', 'ETF', '管理股票', '存託憑證'])]
        return {c: [g] for c, g in ind.items()}
    with _db() as con:
        rows = con.execute('SELECT code, name FROM concepts WHERE label=?', (CONCEPT_LABELS[kind],)).fetchall()
    m = {}
    for c, g in rows:
        m.setdefault(c, []).append(g)
    return m


def sector_ranking(days=1, kind='industry'):
    """類股（產業 / 細產業 / 概念股 / 集團股）漲跌與資金流向。

    - 漲跌：市值加權（同官方類股指數算法）與等權平均；盤中用即時報價。
    - 資金流向：期間成交金額佔全市場比重，與前一段同長度期間相比的變化（百分點）；
      今日則與前 5 日平均比重相比。比重上升 = 資金流入。
    - 法人：期間三大法人（外資、投信）買賣超金額（億）＝ 每日買賣超張數 × 當日收盤。
    """
    f = _load_frames(max(days, 5) * 2 + 10)
    if f is None or len(f['dates']) <= days:
        return None
    meta = f['meta']
    close, vol = f['close'], f['volume']
    snap = _rt_today()
    today = _now().strftime('%Y-%m-%d')
    live = bool(snap and f['dates'][-1] < today)
    turn = close * vol / 1e5                                    # 每檔每日成交金額（億）
    if live:
        q = pd.DataFrame(snap['quotes']).T
        px = pd.to_numeric(q['price'], errors='coerce').reindex(close.columns).fillna(close.iloc[-1])
        lv = pd.to_numeric(q['volume'], errors='coerce').reindex(close.columns).fillna(0)
        turn = pd.concat([turn, (px * lv / 1e5).to_frame(today).T])
        close = pd.concat([close, px.to_frame(today).T])
    now = close.iloc[-1]
    base = close.iloc[-1 - days]
    ret = (now / base - 1) * 100
    cur_turn = turn.iloc[-days:].sum()
    if days == 1:
        prev_turn = turn.iloc[-6:-1]                            # 前 5 日
        prev_tot = prev_turn.sum(axis=1)
        prev_share_of = lambda cols: float((prev_turn[cols].sum(axis=1) / prev_tot).mean() * 100)
    else:
        prev_turn = turn.iloc[-2 * days:-days].sum()
        prev_tot = prev_turn.sum()
        prev_share_of = lambda cols: float(prev_turn[cols].sum() / prev_tot * 100) if prev_tot else None
    tot = cur_turn.sum()
    # 法人買賣超（盤中今天尚未公布，用最近一個已公布的期間）
    inst_n = min(days, len(f['dates']))
    px_inst = f['close'].iloc[-inst_n:]
    inst_amt = {k: (f[k].iloc[-inst_n:] * px_inst * 1000).sum() / 1e8 for k in ('foreign_net', 'trust_net', 'total_net')}
    gmap = _group_map(kind, meta)
    members = {}
    for code, groups in gmap.items():
        if code in close.columns and not code.startswith('00') and pd.notna(ret.get(code)):
            for g in groups:
                members.setdefault(g, []).append(code)
    issued = f['issued']
    out = []
    for g, cols in members.items():
        if len(cols) < 2:
            continue
        r = ret[cols]
        cap_n, cap_b = (now[cols] * issued[cols]).sum(), (base[cols] * issued[cols]).sum()
        cw = (cap_n / cap_b - 1) * 100 if cap_b else None
        share = float(cur_turn[cols].sum() / tot * 100) if tot else None
        ps = prev_share_of(cols)
        order = r.sort_values()
        pick = lambda c: dict(code=c, name=meta.loc[c, 'name'] if c in meta.index else c, ret=round(float(r[c]), 2))
        out.append(dict(name=g, count=len(cols), cap_ret=round(float(cw), 2) if cw is not None and np.isfinite(cw) else None,
                        avg_ret=round(float(r.mean()), 2), up=int((r > 0).sum()), down=int((r < 0).sum()),
                        turnover=round(float(cur_turn[cols].sum()), 1), share=round(share, 2) if share is not None else None,
                        share_chg=round(share - ps, 2) if share is not None and ps is not None else None,
                        foreign=round(float(inst_amt['foreign_net'][cols].sum()), 1),
                        trust=round(float(inst_amt['trust_net'][cols].sum()), 1),
                        inst=round(float(inst_amt['total_net'][cols].sum()), 1),
                        leaders=[pick(c) for c in order.index[::-1][:3]], laggards=[pick(c) for c in order.index[:3]],
                        codes=sorted(cols)))
    out.sort(key=lambda x: -(x['cap_ret'] if x['cap_ret'] is not None else x['avg_ret']))
    dates = list(f['dates']) + ([today] if live else [])
    return dict(days=days, kind=kind, live=live, ts=(snap or {}).get('ts') if live else None,
                date=dates[-1], start=dates[-1 - days] if days > 1 else dates[-1],
                inst_range=[f['dates'][-inst_n], f['dates'][-1]],
                source='Yahoo 股市分類' if kind != 'industry' else '證交所／櫃買中心產業別', sectors=out)


def screen_cobuy(days=5, threshold=0.3, ma_n=20, min_volume=0, include_etf=False, asof=None):
    """外資投信同買：最近 days 天外資、投信持股比例都增加至少 threshold%，且收盤站上 ma_n 日均線。"""
    f = _load_frames(max(days, ma_n) + 10, asof)
    if f is None or len(f['dates']) < max(days, ma_n) + 1:
        return None
    fr = _ratio_change(f, 'foreign_net', days).iloc[-1]
    tr = _ratio_change(f, 'trust_net', days).iloc[-1]
    ma = f['close'].rolling(ma_n).mean().iloc[-1]
    c = f['close'].iloc[-1]
    hit = (fr >= threshold) & (tr >= threshold) & (c > ma) & (f['volume'].iloc[-1] >= min_volume)
    meta = f['meta']
    out = []
    for code in hit[hit.fillna(False)].index:
        if not include_etf and code.startswith('00'):
            continue
        row = _row_base(code, f, meta)
        row.update(foreign_chg=round(float(fr[code]), 2), trust_chg=round(float(tr[code]), 2),
                   ma=round(float(ma[code]), 2), above_ma=round(float((c[code] / ma[code] - 1) * 100), 2),
                   signals=[dict(label='外資投信同買', side='buy')])
        out.append(row)
    return dict(date=f['dates'][-1], start=f['dates'][-days], days=days, threshold=threshold, ma_n=ma_n,
                results=out)


# ── 策略成績單：每種選股條件在歷史資料上的表現 ─────────────────────────
def _strategies(f, cols):
    """名稱 → (方向, 每日是否符合的 DataFrame)。與盤後 / 即時選股的條件一致。"""
    C, H, L, V = (f[k][cols] for k in ('close', 'high', 'low', 'volume'))
    k, d = calc_kd_frame(H, L, C, 9)
    ma = {m: C.rolling(m).mean() for m in (5, 10, 20)}
    vol_up = V > V.shift(1)
    mx = np.maximum(np.maximum(ma[5], ma[10]), ma[20])
    mn = np.minimum(np.minimum(ma[5], ma[10]), ma[20])
    sub = {c: f[c][cols] for c in ('trust_net', 'foreign_net', 'dealer_net', 'total_net')}
    sub['issued'] = f['issued'][cols]
    st = {
        'kdj_buy': ('即時', 'KDJ 買進（K 金叉 D、K<20、量增）', 'buy', _cross_up(k, d) & (k < 20) & vol_up),
        'kdj_sell': ('即時', 'KDJ 賣出（K 死叉 D、K>80、量增）', 'sell', _cross_dn(k, d) & (k > 80) & vol_up),
        'ma_squeeze': ('即時', '均線糾結突破（1%）', 'buy',
                       (ma[20] > ma[20].shift(1)) & ((mx / mn - 1) * 100 <= 1) & _cross_up(C, ma[5]) & vol_up),
        'cobuy': ('盤後', '外資投信同買（5 日各 +0.3%）且站上 MA20', 'buy',
                  (_ratio_change(sub, 'foreign_net', 5) >= 0.3) & (_ratio_change(sub, 'trust_net', 5) >= 0.3) & (C > ma[20])),
    }
    for inv, col, label in (('trust', 'trust_net', '投信'), ('foreign', 'foreign_net', '外資'), ('total', 'total_net', '法人')):
        hold = sub[col].cumsum()
        m5 = hold.rolling(5).mean()
        st[f'{inv}_ma_buy'] = ('盤後', f'{label}持股金叉 5 日均線', 'buy', _cross_up(hold, m5) & (hold > m5))
        st[f'{inv}_ma_sell'] = ('盤後', f'{label}持股死叉 5 日均線', 'sell', _cross_dn(hold, m5) & (hold < m5))
        st[f'{inv}_r20'] = ('盤後', f'{label}持股比例 20 日 +1%', 'buy', _ratio_change(sub, col, 20) >= 1)
        st[f'{inv}_r5'] = ('盤後', f'{label}持股比例 5 日 +2%', 'buy', _ratio_change(sub, col, 5) >= 2)
    return st


SCORECARD_FILE = os.path.join(BASE_DIR, '.tw_scorecard.json')


def build_scorecard(min_avg_vol=500):
    """所有策略在歷史資料上的表現（分前、後兩段），以及今日符合的股票。

    - 只看 20 日均量 ≥ min_avg_vol 張的股票（避免冷門股）。
    - 訊號只算條件剛成立的那一天；報酬為之後 5／10／20 個交易日，未還原除權息。
    - 超額 = 減去同一天全市場平均報酬；勝過大盤 = 超額為正的比例（SELL 為負）。
    """
    import warnings
    f = _load_frames(BACKFILL_DAYS + 10)
    if f is None or len(f['dates']) < 120:
        return None
    cols = [c for c in f['close'].columns if not c.startswith('00')]
    C, V = f['close'][cols], f['volume'][cols]
    liq = (V.rolling(20).mean() >= min_avg_vol)
    n = len(C)
    warm, half = 60, (60 + n) // 2
    periods = {'first': (warm, half), 'second': (half, n), 'all': (warm, n)}
    meta = f['meta']
    out = dict(start=f['dates'][warm], split=f['dates'][half], end=f['dates'][-1], days=n - warm,
               min_avg_vol=min_avg_vol, horizons={}, today={}, baseline={})
    strategies = _strategies(f, cols)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        for h in (5, 10, 20):
            fwd = (C.shift(-h) / C - 1).values
            fwd_liq = np.where(liq.values, fwd, np.nan)
            ex = fwd - np.nanmean(fwd_liq, axis=1, keepdims=True)
            out['baseline'][h] = {}
            for p, (a, b) in periods.items():
                v = fwd_liq[a:b]
                v = v[~np.isnan(v)]                      # 沒有未來報酬的不算
                out['baseline'][h][p] = dict(win=round(float((v > 0).mean() * 100), 1), avg=round(float(v.mean() * 100), 2))
            rows = []
            for sid, (kind, name, side, sig) in strategies.items():
                sig = sig.fillna(False).astype(bool) & liq
                trig = (sig & ~sig.shift(1).fillna(False).astype(bool)).values
                sgn = 1 if side == 'buy' else -1
                stats = {}
                for p, (a, b) in periods.items():
                    m = trig.copy()
                    m[:a] = False
                    m[b:] = False
                    r, x = fwd[m], ex[m]
                    ok = ~np.isnan(r)
                    r, x = r[ok], x[ok]
                    stats[p] = None if not len(r) else dict(
                        n=int(len(r)), win=round(float((r * sgn > 0).mean() * 100), 1),
                        avg=round(float(r.mean() * 100), 2), excess=round(float(x.mean() * 100), 2),
                        beat=round(float((x * sgn > 0).mean() * 100), 1))
                good = lambda s: s is not None and (s['excess'] > 0 if side == 'buy' else s['excess'] < 0)
                stable = good(stats['first']) and good(stats['second'])
                worst = min((s['excess'] * sgn for s in (stats['first'], stats['second']) if s), default=-99)
                rows.append(dict(id=sid, kind=kind, name=name, side=side, stats=stats, stable=stable,
                                 score=round(worst, 2)))
            rows.sort(key=lambda r: -r['score'])
            out['horizons'][h] = rows
        # 今日符合（條件目前成立，不限剛成立）
        last_liq = liq.iloc[-1]
        for sid, (kind, name, side, sig) in strategies.items():
            today = sig.iloc[-1].fillna(False).astype(bool) & last_liq
            codes = list(today[today].index)
            codes.sort(key=lambda c: -float(V[c].iloc[-1]))
            out['today'][sid] = dict(count=len(codes), stocks=[dict(code=c, name=meta.loc[c, 'name'] if c in meta.index else c,
                                      close=round(float(C[c].iloc[-1]), 2),
                                      chg_pct=round(float(f['chg'][c].iloc[-1] / (C[c].iloc[-1] - f['chg'][c].iloc[-1]) * 100), 2)
                                      if pd.notna(f['chg'][c].iloc[-1]) and C[c].iloc[-1] != f['chg'][c].iloc[-1] else None,
                                      volume=round(float(V[c].iloc[-1])))
                                 for c in codes[:60]])
    out['version'] = list(_data_version())
    return out


def get_scorecard():
    """讀快取（資料版本相同就沿用），沒有或過期才重算並寫回檔案供其他 worker 共用。"""
    ver = list(_data_version())
    try:
        with open(SCORECARD_FILE, encoding='utf-8') as fh:
            cached = json.load(fh)
        if cached.get('version') == ver:
            return cached
    except (OSError, ValueError):
        pass
    res = build_scorecard()
    if res:
        with open(SCORECARD_FILE + '.tmp', 'w', encoding='utf-8') as fh:
            json.dump(res, fh, ensure_ascii=False)
        os.replace(SCORECARD_FILE + '.tmp', SCORECARD_FILE)
    return res


# ── LINE 推播（即時選股訊號）─────────────────────────────────────────
_alert_lock = threading.Lock()


def _default_alerts():
    return {'enabled': False, 'scope': 'watch', 'rules': list(RT_RULES), 'params': {},
            'include_etf': False, 'sent': {}, 'log': []}


def _load_alerts(user):
    return userdata.get(user, 'tw_alerts') or _default_alerts()


def _save_alerts(user, cfg):
    userdata.put(user, 'tw_alerts', cfg)


def _line_creds(user):
    c = userdata.get(user, 'line') or {}
    return c.get('token', ''), c.get('user_id', '')


def _push_line(text, user):
    token, uid = _line_creds(user)
    if not token or not uid:
        return False, '尚未設定 LINE Channel Token 與 User ID'
    try:
        r = requests.post('https://api.line.me/v2/bot/message/push', timeout=10,
                          headers={'Authorization': f'Bearer {token}'},
                          json={'to': uid, 'messages': [{'type': 'text', 'text': text[:4900]}]})
        return r.ok, ('' if r.ok else f'LINE 回應 {r.status_code}：{r.text[:200]}')
    except Exception as e:
        return False, str(e)


def _scope_codes(scope, user):
    if scope == 'all':
        return None
    groups = _load_watch(user)['groups']
    if scope == 'watch':
        return {c for g in groups for c in g['codes']}
    return {c for g in groups if g['name'] == scope for c in g['codes']}


def _check_alerts():
    """盤中每次更新報價後，替每個開啟推播的帳號各跑一次。"""
    for user, cfg in userdata.users_with('tw_alerts'):
        if cfg.get('enabled'):
            try:
                _check_alerts_user(user)
            except Exception as e:
                print(f'[tw_board] alerts {user}: {e}')


def _check_alerts_user(user):
    """新出現的訊號推播到這個帳號的 LINE（同一檔同一條件一天只推一次）。"""
    with _alert_lock:
        cfg = _load_alerts(user)
        if not cfg.get('enabled'):
            return
        res = screen_realtime(cfg.get('rules') or list(RT_RULES), cfg.get('params') or {},
                              include_etf=cfg.get('include_etf', False))
        if not res or res.get('source') != 'realtime':
            return
        allow = _scope_codes(cfg.get('scope', 'watch'), user)
        today = _now().strftime('%Y-%m-%d')
        sent = set(cfg.get('sent', {}).get(today, []))
        new = []
        for r in res['results']:
            if allow is not None and r['code'] not in allow:
                continue
            for sig in r['signals']:
                key = f"{r['code']}:{sig['rule']}"
                if key not in sent:
                    sent.add(key)
                    new.append((r, sig))
        if not new:
            return
        lines = [f"【台股即時選股】{_now():%H:%M}"]
        for r, sig in new[:30]:
            lines.append(f"{'🔴 BUY' if sig['side'] == 'buy' else '🟢 SELL'} {r['code']} {r['name']} "
                         f"{r['close']}（{'+' if (r.get('chg_pct') or 0) > 0 else ''}{r.get('chg_pct')}%）"
                         f" K{r['k']} D{r['d']}")
        if len(new) > 30:
            lines.append(f"…另有 {len(new) - 30} 檔")
        ok, err = _push_line('\n'.join(lines), user)
        cfg['sent'] = {today: sorted(sent)}
        log = cfg.get('log', [])
        for r, sig in new:
            log.append(dict(ts=_now().strftime('%Y-%m-%d %H:%M'), code=r['code'], name=r['name'],
                            side=sig['side'], rule=sig['rule'], price=r['close'], pushed=ok))
        cfg['log'] = log[-100:]
        cfg['last_error'] = err
        _save_alerts(user, cfg)


# 即時選股條件；新增條件只要在這裡加一個函式並登錄到 RT_RULES。
# 每個條件拿到 c（k、d、close、vol、vol_prev，最後一列 = 今天／盤中）與使用者參數 p
def _rt_kdj_buy(c, p):
    k, d = c['k'], c['d']
    return _cross_up(k, d).iloc[-1] & (k.iloc[-1] < p.get('k_low', 20)) & (c['vol'] > c['vol_prev'])


def _rt_kdj_sell(c, p):
    k, d = c['k'], c['d']
    return _cross_dn(k, d).iloc[-1] & (k.iloc[-1] > p.get('k_high', 80)) & (c['vol'] > c['vol_prev'])


def _rt_ma_squeeze(c, p):
    """均線糾結後突破：MA20 上揚、MA5/10/20 彼此相差在 band% 內、股價向上穿越 MA5、量 > 昨日量。"""
    close = c['close']
    ma5, ma10, ma20 = (close.rolling(n).mean() for n in (5, 10, 20))
    band = p.get('ma_band', 1.0)
    hi = pd.concat([ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]], axis=1).max(axis=1)
    lo = pd.concat([ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]], axis=1).min(axis=1)
    squeeze = (hi / lo - 1) * 100 <= band
    ma20_up = ma20.iloc[-1] > ma20.iloc[-2]
    cross = (close.iloc[-2] <= ma5.iloc[-2]) & (close.iloc[-1] > ma5.iloc[-1])
    return ma20_up & squeeze & cross & (c['vol'] > c['vol_prev'])


RT_RULES = {
    'kdj_buy': dict(label='KDJ 買進：K 金叉 D 且 K<20 且成交量 > 昨日成交量', side='buy', fn=_rt_kdj_buy),
    'kdj_sell': dict(label='KDJ 賣出：K 死叉 D 且 K>80 且成交量 > 昨日成交量', side='sell', fn=_rt_kdj_sell),
    'ma_squeeze': dict(label='均線糾結突破：MA20 上揚 且 MA5/10/20 糾結 且 股價穿越 MA5 且成交量 > 昨日成交量',
                       side='buy', fn=_rt_ma_squeeze),
}


def screen_realtime(rule_ids, params, include_etf=False):
    kd_n = int(params.get('kd_n', 9))
    f = _load_frames(kd_n + 60)
    if f is None:
        return None
    snap = _rt_today()
    today = _now().strftime('%Y-%m-%d')
    high, low, close, volume = f['high'], f['low'], f['close'], f['volume']
    source = 'close'
    if snap and f['dates'][-1] < today:
        # 盤中：把即時報價當作今天這根 K 棒接在歷史資料後面
        q = pd.DataFrame(snap['quotes']).T
        q = q[q['price'].notna()].reindex(close.columns)
        for name, frame, key in (('high', high, 'high'), ('low', low, 'low'),
                                 ('close', close, 'price'), ('volume', volume, 'volume')):
            row = pd.to_numeric(q[key], errors='coerce')
            if name != 'volume':
                row = row.fillna(frame.iloc[-1])
            else:
                row = row.fillna(0)
            frame.loc[today] = row
        high, low, close, volume = f['high'], f['low'], f['close'], f['volume']
        prev_close = close.iloc[-2]
        f['chg'].loc[today] = close.iloc[-1] - prev_close
        source = 'realtime'
    k, d = calc_kd_frame(high, low, close, kd_n)
    vol = volume.iloc[-1]
    vol_prev = volume.iloc[-2]                     # 昨日成交量
    ctx = dict(k=k, d=d, close=close, vol=vol, vol_prev=vol_prev)
    ma = {n: close.rolling(n).mean().iloc[-1] for n in (5, 10, 20)}
    out = {}
    for rid in rule_ids:
        rule = RT_RULES.get(rid)
        if not rule:
            continue
        hit = rule['fn'](ctx, params)
        for code in hit[hit.fillna(False).astype(bool)].index:
            if not include_etf and code.startswith('00'):
                continue
            row = out.get(code) or _row_base(code, f, f['meta'])
            row.setdefault('signals', []).append(dict(rule=rid, label=rule['label'],
                                                      side=rule['side']))
            row.update(k=round(float(k[code].iloc[-1]), 1), d=round(float(d[code].iloc[-1]), 1),
                       j=round(float(3 * k[code].iloc[-1] - 2 * d[code].iloc[-1]), 1),
                       vol_prev=round(float(vol_prev[code])),
                       ma5=round(float(ma[5][code]), 2), ma10=round(float(ma[10][code]), 2),
                       ma20=round(float(ma[20][code]), 2),
                       ma_spread=round(float((max(ma[5][code], ma[10][code], ma[20][code]) /
                                              min(ma[5][code], ma[10][code], ma[20][code]) - 1) * 100), 2))
            out[code] = row
    return dict(date=close.index[-1], source=source, ts=(snap or {}).get('ts'),
                results=list(out.values()))


# ── 自選股 ────────────────────────────────────────────────────────────
_watch_lock = threading.Lock()


DEFAULT_GROUP = '自選股'


def _load_watch(user):
    """{'groups': [{'name': ..., 'codes': [...]}, ...]}；舊版的單一清單自動轉成預設群組。"""
    data = userdata.get(user, 'tw_watchlist') or []
    if isinstance(data, list):
        data = {'groups': [{'name': DEFAULT_GROUP, 'codes': data}]}
    if not data.get('groups'):
        data['groups'] = [{'name': DEFAULT_GROUP, 'codes': []}]
    return data


def _save_watch(user, data):
    userdata.put(user, 'tw_watchlist', data)


def _watch_payload(data):
    codes = {c for g in data['groups'] for c in g['codes']}
    names = {}
    if codes:
        with _db() as con:
            q = ','.join('?' * len(codes))
            names = dict(con.execute(f'SELECT code, name FROM stocks WHERE code IN ({q})',
                                     list(codes)))
    return dict(groups=data['groups'], names={c: names.get(c, c) for c in codes})


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/tw/board/status')
def api_status():
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = dict(_status)
    with _db() as con:
        st['trading_days'] = _trading_days_loaded()
        st['latest'] = con.execute("SELECT MAX(date) FROM sync_log WHERE source='twse_px' "
                                   "AND status='ok'").fetchone()[0]
        st['latest_inst'] = con.execute("SELECT MAX(date) FROM sync_log WHERE "
                                        "source='twse_inst' AND status='ok'").fetchone()[0]
    st['market_open'] = _market_open()
    return jsonify(st)


def _turnover(market, since=None):
    """大盤成交金額（億元）＝ 個股收盤 × 成交量 加總。"""
    with _db() as con:
        q = ('SELECT d.date, SUM(d.close * d.volume) / 100000.0 FROM daily d JOIN stocks s ON s.code = d.code '
             'WHERE s.market = ?' + (' AND d.date >= ?' if since else '') + ' GROUP BY d.date')
        return dict(con.execute(q, (market, since) if since else (market,)))


def _index_quote(code):
    """指數最新報價：盤中用 MIS 即時，否則用官方日資料。"""
    meta = INDEX_META[code]
    with _db() as con:
        rows = con.execute('SELECT date, open, high, low, close FROM index_daily WHERE code=? '
                           'ORDER BY date DESC LIMIT 2', (code,)).fetchall()
    res = dict(code=code, name=meta['name'], market=meta['market'], industry='大盤指數', kind='index',
               realtime=False)
    if rows:
        d, o, h, l, c = rows[0]
        prev = rows[1][4] if len(rows) > 1 else None
        res.update(date=d, open=o, high=h, low=l, price=c, prev=prev)
    snap = _rt_today()
    q = (snap or {}).get('quotes', {}).get(meta['mis'])
    if q and q.get('price') and (not rows or rows[0][0] < _now().strftime('%Y-%m-%d')):
        res.update(open=q['open'], high=q['high'], low=q['low'], price=q['price'], prev=q['prev'],
                   realtime=True, time=q.get('time'), date=_now().strftime('%Y-%m-%d'))
    if res.get('price') is not None and res.get('prev'):
        res['chg'] = round(res['price'] - res['prev'], 2)
        res['chg_pct'] = round(res['chg'] / res['prev'] * 100, 2)
    res['volume'] = round(_turnover(meta['market'], res.get('date')).get(res.get('date'), 0) or 0) or None
    return res


_list_cache = {}


def _list_base():
    """收盤資料部分只在資料更新時重查（約 0.6 秒 → 0）。"""
    ver = _data_version()
    hit = _list_cache.get('base')
    if hit and hit[0] == ver:
        return hit[1], hit[2], hit[3]
    with _db() as con:
        latest = con.execute("SELECT MAX(date) FROM sync_log WHERE source='twse_px' AND status='ok'"
                             ).fetchone()[0]
        if not latest:
            return None, [], {}
        rows = con.execute(
            'SELECT s.code, s.name, s.market, s.industry, s.kind, d.open, d.high, d.low, d.close, '
            'd.chg, d.volume FROM stocks s LEFT JOIN daily d ON d.code=s.code AND d.date=? ',
            (latest,)).fetchall()
        # 當天沒成交的股票：用最近 30 天內最後一筆收盤
        # SQLite：與 MAX() 一起選的欄位取自日期最大的那一列
        last_close = {c: v for c, v, _ in con.execute(
            "SELECT code, close, MAX(date) FROM daily WHERE date > date(?, '-45 days') "
            'GROUP BY code', (latest,))}
    _list_cache['base'] = (ver, latest, rows, last_close)
    return latest, rows, last_close


@bp.route('/api/tw/board/list')
def api_list():
    _touch_active()
    latest, rows, last_close = _list_base()
    if not latest:
        return jsonify(dict(date=None, rows=[]))
    snap = _rt_today()
    rt = snap['quotes'] if snap and latest < _now().strftime('%Y-%m-%d') else {}
    out = []
    for code, name, market, ind, kind, o, h, l, c, chg, vol in rows:
        prev = c - chg if c is not None and chg is not None else None
        q = rt.get(code)
        if q and q.get('price'):
            o, h, l, c, vol, prev = q['open'], q['high'], q['low'], q['price'], q['volume'], q['prev']
            chg = c - prev if prev else None
        elif c is None:
            c = last_close.get(code)
        out.append([code, name, market, ind, kind, c,
                    round(chg, 2) if chg is not None else None,
                    round(chg / prev * 100, 2) if chg is not None and prev else None,
                    vol, o, h, l, prev])
    idx_rows = []
    for code in INDEX_META:
        q = _index_quote(code)
        idx_rows.append([code, q['name'], q['market'], '大盤指數', 'index', q.get('price'), q.get('chg'),
                         q.get('chg_pct'), q.get('volume'), q.get('open'), q.get('high'), q.get('low'),
                         q.get('prev')])
    out = idx_rows + out
    age = None
    if rt and snap.get('ts'):
        age = int((_now().tz_localize(None) - pd.Timestamp(snap['ts'])).total_seconds())
    return jsonify(dict(date=latest, realtime=bool(rt), ts=(snap or {}).get('ts') if rt else None,
                        rt_age=age, rt_partial=bool(rt and snap.get('failed_chunks')),
                        market_open=_market_open(),
                        fields=['code', 'name', 'market', 'industry', 'kind', 'price', 'chg',
                                'chg_pct', 'volume', 'open', 'high', 'low', 'prev'],
                        rows=out))


@bp.route('/api/tw/board/quote/<code>')
def api_quote(code):
    _touch_active()
    if code in INDEX_META:
        return jsonify(_index_quote(code))
    with _db() as con:
        meta = con.execute('SELECT code, name, market, industry FROM stocks WHERE code=?',
                           (code,)).fetchone()
        last = con.execute('SELECT date, open, high, low, close, chg, volume FROM daily '
                           'WHERE code=? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    if not meta:
        return jsonify(error='查無此代碼'), 404
    res = dict(code=code, name=meta[1], market=meta[2], industry=meta[3], realtime=False)
    if last:
        prev = last[4] - last[5] if last[5] is not None else None
        res.update(date=last[0], open=last[1], high=last[2], low=last[3], price=last[4],
                   chg=last[5], prev=prev, volume=last[6])
    snap = _rt_today()
    q = (snap or {}).get('quotes', {}).get(code)
    if q and q.get('price') and (not last or last[0] < _now().strftime('%Y-%m-%d')):
        res.update(open=q['open'], high=q['high'], low=q['low'], price=q['price'],
                   prev=q['prev'], volume=q['volume'], realtime=True, time=q.get('time'),
                   date=_now().strftime('%Y-%m-%d'))
        res['chg'] = q['price'] - q['prev'] if q.get('prev') else None
    if res.get('chg') is not None and res.get('prev'):
        res['chg_pct'] = round(res['chg'] / res['prev'] * 100, 2)
    return jsonify(res)


def _yf_bars(code, market, period):
    return _yf_bars_symbol(f"{code}.{'TW' if market == 'TSE' else 'TWO'}", period)


def _yf_bars_symbol(sym, period):
    # 週 / 月線由日線自己合成：yfinance 的週線常缺本週
    cfg = PERIODS[period]
    key = f'tw_board_bars:{sym}:{period}'
    hit = _bar_cache.get(key)
    if hit and time.time() - hit[0] < cfg[2]:
        return hit[1]
    h = yf.Ticker(sym).history(period=cfg[0], interval=cfg[1], auto_adjust=False)
    if h is None or h.empty:
        return []
    h = h.dropna(subset=['Open', 'High', 'Low', 'Close'])
    if period in ('W', 'M'):
        h.index = h.index.tz_localize(None) if h.index.tz is not None else h.index
        wk = h.index.to_period('W-SUN' if period == 'W' else 'M')
        h = h.groupby(wk).agg(Open=('Open', 'first'), High=('High', 'max'), Low=('Low', 'min'),
                              Close=('Close', 'last'), Volume=('Volume', 'sum'),
                              first=('Open', lambda x: x.index[0]))
        h.index = pd.DatetimeIndex(h.pop('first'))   # 以該週 / 該月第一個交易日標示
    bars = []
    for ts, r in h.iterrows():
        if period in INTRADAY:
            t = ts.tz_convert('Asia/Taipei') if ts.tzinfo else ts
            # lightweight-charts 以 UTC 顯示；把台北牆上時間當成 UTC，圖上看到的就是台北時間
            tv = int(t.tz_localize(None).timestamp())
            day = t.strftime('%Y-%m-%d')
        else:
            tv = day = ts.strftime('%Y-%m-%d')
        bars.append(dict(time=tv, day=day, open=round(float(r['Open']), 2),
                         high=round(float(r['High']), 2), low=round(float(r['Low']), 2),
                         close=round(float(r['Close']), 2),
                         volume=round(float(r['Volume']) / 1000)))
    if len(_bar_cache) > 300:
        _bar_cache.clear()
    _bar_cache[key] = (time.time(), bars)
    return bars


_bar_cache = {}
# 週期 → (yfinance period, interval, 快取秒數)
PERIODS = {'1': ('7d', '1m', 30), '15': ('60d', '15m', 60), '60': ('2y', '60m', 120),
           'D': ('5y', '1d', 600), 'W': ('15y', '1d', 1800), 'M': ('max', '1d', 3600)}
INTRADAY = ('1', '15', '60')


def _db_bars(code):
    with _db() as con:
        rows = con.execute('SELECT date, open, high, low, close, volume FROM daily WHERE code=? '
                           'ORDER BY date', (code,)).fetchall()
    return [dict(time=d, day=d, open=o, high=h, low=l, close=c, volume=round(v or 0))
            for d, o, h, l, c, v in rows if o is not None]


def _resample(bars, period):
    """日線 → 週 / 月線（以該期第一個交易日標示，量為加總）。"""
    if period == 'D' or not bars:
        return bars
    df = pd.DataFrame(bars)
    df['p'] = pd.to_datetime(df['day']).dt.to_period('W-SUN' if period == 'W' else 'M')
    g = df.groupby('p', sort=True).agg(day=('day', 'first'), open=('open', 'first'), high=('high', 'max'),
                                      low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'))
    return [dict(time=r.day, day=r.day, open=r.open, high=r.high, low=r.low, close=r.close,
                 volume=round(r.volume)) for r in g.itertuples()]


def _index_chart(code, period):
    meta = INDEX_META[code]
    with _db() as con:
        rows = con.execute('SELECT date, open, high, low, close FROM index_daily WHERE code=? ORDER BY date',
                           (code,)).fetchall()
        inst = con.execute('SELECT date, foreign_amt, trust_amt, dealer_amt, total_amt FROM market_inst '
                           'WHERE market=? ORDER BY date', (meta['market'],)).fetchall()
    turnover = _turnover(meta['market'])
    source = 'twse' if code == 'TAIEX' else 'tpex'
    if period in INTRADAY:
        if not meta['yf']:
            return None, '櫃買指數沒有公開的盤中歷史資料，請改看日線以上'
        bars = _yf_bars_symbol(meta['yf'], period)
        source = 'yfinance'
    else:
        bars = [dict(time=d, day=d, open=o, high=h, low=l, close=c, volume=round(turnover.get(d) or 0))
                for d, o, h, l, c in rows if c is not None]
        q = _index_quote(code)
        if q.get('realtime') and bars and q['date'] > bars[-1]['day']:   # 盤中：把今天接上去
            bars.append(dict(time=q['date'], day=q['date'], open=q['open'], high=q['high'], low=q['low'],
                             close=q['price'], volume=0))
        bars = _resample(bars, period)
    days = [r[0] for r in inst]
    cum = [0.0] * 4
    hold = {k: [] for k in ('foreign', 'trust', 'dealer', 'total')}
    for r in inst:
        for i, k in enumerate(('foreign', 'trust', 'dealer', 'total')):
            cum[i] += r[i + 1] or 0
            hold[k].append(round(cum[i], 2))
    nets = {k: [round(r[i + 1] or 0, 2) for r in inst] for i, k in enumerate(('foreign', 'trust', 'dealer', 'total'))}
    return dict(code=code, name=meta['name'], market=meta['market'], kind='index', unit='億', period=period,
                source=source, bars=bars, inst=dict(days=days, hold=hold, net=nets, foreign_anchor=False)), None


@bp.route('/api/tw/board/chart/<code>')
def api_chart(code):
    period = request.args.get('period', 'D')
    if period not in PERIODS:
        return jsonify(error='period 必須是 1 / 15 / 60 / D / W / M'), 400
    if code in INDEX_META:
        res, err = _index_chart(code, period)
        return (jsonify(error=err), 400) if err else jsonify(res)
    with _db() as con:
        meta = con.execute('SELECT name, market FROM stocks WHERE code=?', (code,)).fetchone()
        inst = con.execute('SELECT date, foreign_net, trust_net, dealer_net, total_net FROM inst '
                           'WHERE code=? ORDER BY date', (code,)).fetchall()
        inst_days = [r[0] for r in con.execute(
            "SELECT date FROM sync_log WHERE source=? AND status='ok' ORDER BY date",
            ('twse_inst' if not meta or meta[1] == 'TSE' else 'tpex_inst',))]
        fh = con.execute('SELECT date, lots FROM foreign_hold WHERE code=?', (code,)).fetchone()
        iss = con.execute('SELECT lots FROM issued WHERE code=?', (code,)).fetchone()
    if not meta:
        return jsonify(error='查無此代碼'), 404
    try:
        bars = _yf_bars(code, meta[1], period)
    except Exception as e:
        print(f'[tw_board] yf {code}: {e}')
        bars = []
    source = 'yfinance'
    if not bars and period == 'D':
        bars, source = _db_bars(code), 'db'

    # 法人持股(張)：按交易日補 0 後累計
    by_day = {r[0]: r[1:] for r in inst}
    hold = {'foreign': [], 'trust': [], 'dealer': [], 'total': []}
    cum = [0.0, 0.0, 0.0, 0.0]
    for day in inst_days:
        v = by_day.get(day, (0, 0, 0, 0))
        for i in range(4):
            cum[i] += v[i] or 0
        for i, k in enumerate(('foreign', 'trust', 'dealer', 'total')):
            hold[k].append(round(cum[i], 1))
    foreign_anchor = False
    if fh and fh[0] in inst_days:
        # 外資：以最新實際持股為錨點
        off = fh[1] - hold['foreign'][inst_days.index(fh[0])]
        hold['foreign'] = [round(x + off, 1) for x in hold['foreign']]
        foreign_anchor = True
    nets = {k: [round((by_day.get(day, (0, 0, 0, 0))[i] or 0), 1) for day in inst_days]
            for i, k in enumerate(('foreign', 'trust', 'dealer', 'total'))}
    return jsonify(dict(code=code, name=meta[0], market=meta[1], period=period, source=source,
                        bars=bars, issued=iss[0] if iss else None,
                        inst=dict(days=inst_days, hold=hold, net=nets, foreign_anchor=foreign_anchor)))


@bp.route('/api/tw/board/indices')
def api_indices():
    """加權 / 櫃買指數：最新報價、近 60 日收盤（走勢小圖）、當日三大法人買賣超（億元）。"""
    out = []
    with _db() as con:
        for code, meta in INDEX_META.items():
            q = _index_quote(code)
            closes = [r[0] for r in con.execute(
                'SELECT close FROM (SELECT date, close FROM index_daily WHERE code=? ORDER BY date DESC LIMIT 60) '
                'ORDER BY date', (code,))]
            if q.get('realtime') and q.get('price'):
                closes.append(q['price'])
            inst = con.execute('SELECT date, foreign_amt, trust_amt, dealer_amt, total_amt FROM market_inst '
                               'WHERE market=? ORDER BY date DESC LIMIT 1', (meta['market'],)).fetchone()
            q.update(spark=closes, inst=dict(zip(('date', 'foreign', 'trust', 'dealer', 'total'), inst)) if inst else None)
            out.append(q)
    return jsonify(out)


@bp.route('/api/tw/board/watchlist', methods=['GET', 'POST', 'DELETE'])
def api_watchlist():
    """GET：所有群組；POST {code, group}：加入；DELETE {code, group?}：移出（不給 group = 從所有群組移出）。"""
    with _watch_lock:
        user = userdata.current()
        data = _load_watch(user)
        if request.method == 'GET':
            return jsonify(_watch_payload(data))
        body = request.get_json(silent=True) or {}
        code = str(body.get('code', '')).strip()
        gname = body.get('group')
        if not CODE_RE.match(code):
            return jsonify(error='代碼格式錯誤'), 400
        groups = data['groups']
        if request.method == 'POST':
            g = next((g for g in groups if g['name'] == gname), groups[0])
            if code not in g['codes']:
                g['codes'].append(code)
        else:
            for g in groups:
                if (gname is None or g['name'] == gname) and code in g['codes']:
                    g['codes'].remove(code)
        _save_watch(user, data)
        return jsonify(_watch_payload(data))


@bp.route('/api/tw/board/watchlist/groups', methods=['POST'])
def api_watch_groups():
    """{action: add | rename | delete | reorder, name, new_name, codes}"""
    b = request.get_json(silent=True) or {}
    action, name = b.get('action'), str(b.get('name', '')).strip()
    with _watch_lock:
        user = userdata.current()
        data = _load_watch(user)
        groups = data['groups']
        names = [g['name'] for g in groups]
        if action == 'add':
            if not name or len(name) > 20 or name in names:
                return jsonify(error='群組名稱不可空白、重複或超過 20 字'), 400
            groups.append({'name': name, 'codes': []})
        elif action == 'rename':
            new = str(b.get('new_name', '')).strip()
            if name not in names or not new or len(new) > 20 or new in names:
                return jsonify(error='群組名稱不可空白、重複或超過 20 字'), 400
            groups[names.index(name)]['name'] = new
        elif action == 'delete':
            if name not in names or len(groups) == 1:
                return jsonify(error='至少要保留一個群組'), 400
            groups.pop(names.index(name))
        elif action == 'reorder':
            if name not in names:
                return jsonify(error='查無群組'), 400
            g = groups[names.index(name)]
            g['codes'] = [c for c in b.get('codes', []) if CODE_RE.match(str(c))]
        else:
            return jsonify(error='未知的動作'), 400
        _save_watch(user, data)
        return jsonify(_watch_payload(data))


@bp.route('/api/tw/board/screen/after', methods=['POST'])
def api_screen_after():
    b = request.get_json(silent=True) or {}
    rules = [r for r in b.get('rules', []) if r.get('investor') in INVESTORS
             and r.get('side') in ('buy', 'sell')]
    if not rules:
        return jsonify(error='請至少選擇一個條件'), 400
    try:
        res = screen_after_hours(rules, ma_n=max(2, min(60, int(b.get('ma_n', 5)))),
                                 within=max(1, min(10, int(b.get('within', 1)))),
                                 kd_n=max(3, min(30, int(b.get('kd_n', 9)))),
                                 min_volume=float(b.get('min_volume', 0) or 0),
                                 include_etf=bool(b.get('include_etf')),
                                 asof=b.get('asof') or None)
    except (TypeError, ValueError) as e:
        return jsonify(error=f'參數錯誤：{e}'), 400
    if res is None:
        return jsonify(error='歷史資料尚未載入完成，請稍後再試'), 503
    return jsonify(res)


@bp.route('/api/tw/board/backtest', methods=['POST'])
def api_backtest():
    b = request.get_json(silent=True) or {}
    rules = [r for r in b.get('rules', []) if r.get('investor') in INVESTORS
             and r.get('side') in ('buy', 'sell')]
    if not rules:
        return jsonify(error='請至少選擇一個條件'), 400
    try:
        ma_list = sorted({max(2, min(60, int(x))) for x in (b.get('ma_list') or [3, 5])})[:4]
        res = backtest_after_hours(rules, ma_list=ma_list,
                                   within=max(1, min(10, int(b.get('within', 1)))),
                                   kd_n=max(3, min(30, int(b.get('kd_n', 9)))),
                                   min_volume=float(b.get('min_volume', 0) or 0),
                                   include_etf=bool(b.get('include_etf')))
    except (TypeError, ValueError) as e:
        return jsonify(error=f'參數錯誤：{e}'), 400
    if res is None:
        return jsonify(error='歷史資料不足（至少需要 60 個交易日）'), 503
    return jsonify(res)


@bp.route('/api/tw/board/inst_rank')
def api_inst_rank():
    a = request.args
    investor = a.get('investor', 'total')
    if investor not in INVESTORS:
        return jsonify(error='investor 必須是 trust / foreign / dealer / total'), 400
    try:
        days = int(a.get('days', 1))
        limit = int(a.get('limit', 50))
    except ValueError:
        return jsonify(error='days / limit 必須是整數'), 400
    if days not in (1, 3, 5, 10, 20):
        return jsonify(error='days 必須是 1 / 3 / 5 / 10 / 20'), 400
    res = inst_ranking(investor, 'sell' if a.get('side') == 'sell' else 'buy', days,
                       'amount' if a.get('by') == 'amount' else 'lots', max(10, min(200, limit)),
                       a.get('etf') == '1', a.get('market') if a.get('market') in ('TSE', 'OTC') else '')
    if res is None:
        return jsonify(error='法人資料尚未載入完成'), 503
    return jsonify(res)


def _ratio_rules(b):
    return [r for r in b.get('rules', []) if r.get('investor') in INVESTORS and r.get('side') in ('buy', 'sell')]


@bp.route('/api/tw/board/screen/ratio', methods=['POST'])
def api_screen_ratio():
    b = request.get_json(silent=True) or {}
    rules = _ratio_rules(b)
    if not rules:
        return jsonify(error='請至少選擇一個條件'), 400
    try:
        res = screen_ratio(rules, days=max(1, min(60, int(b.get('days', 5)))),
                           threshold=max(0.01, min(50, float(b.get('threshold', 1)))),
                           min_volume=float(b.get('min_volume', 0) or 0),
                           include_etf=bool(b.get('include_etf')), asof=b.get('asof') or None)
    except (TypeError, ValueError) as e:
        return jsonify(error=f'參數錯誤：{e}'), 400
    if res is None:
        return jsonify(error='歷史資料尚未載入完成'), 503
    return jsonify(res)


@bp.route('/api/tw/board/optimize/ratio', methods=['POST'])
def api_optimize_ratio():
    b = request.get_json(silent=True) or {}
    rules = _ratio_rules(b)
    if not rules:
        return jsonify(error='請至少選擇一個條件'), 400
    try:
        days_list = sorted({max(1, min(60, int(x))) for x in (b.get('days_list') or [3, 5, 10, 20])})[:6]
        th_list = sorted({max(0.05, min(20, float(x))) for x in (b.get('th_list') or [0.5, 1, 2, 3])})[:6]
        horizon = int(b.get('horizon', 10))
        if horizon not in (5, 10, 20):
            return jsonify(error='horizon 必須是 5 / 10 / 20'), 400
        res = optimize_ratio(rules, days_list, th_list, horizon=horizon,
                             objective=b.get('objective') if b.get('objective') in ('avg', 'excess', 'win', 'beat') else 'excess',
                             min_signals=max(5, min(500, int(b.get('min_signals', 30)))),
                             min_volume=float(b.get('min_volume', 0) or 0), include_etf=bool(b.get('include_etf')))
    except (TypeError, ValueError) as e:
        return jsonify(error=f'參數錯誤：{e}'), 400
    if res is None:
        return jsonify(error='歷史資料不足，無法最佳化'), 503
    return jsonify(res)


@bp.route('/api/tw/board/sectors')
def api_sectors():
    _touch_active()
    try:
        days = int(request.args.get('days', 1))
    except ValueError:
        days = 1
    if days not in (1, 5, 10, 20, 60):
        return jsonify(error='days 必須是 1 / 5 / 10 / 20 / 60'), 400
    kind = request.args.get('kind', 'industry')
    if kind not in ('industry', 'sub', 'concept', 'group'):
        return jsonify(error='kind 必須是 industry / sub / concept / group'), 400
    res = sector_ranking(days, kind)
    if res is None:
        return jsonify(error='歷史資料尚未載入完成'), 503
    return jsonify(res)


@bp.route('/api/tw/board/screen/cobuy', methods=['POST'])
def api_screen_cobuy():
    b = request.get_json(silent=True) or {}
    try:
        res = screen_cobuy(days=max(1, min(60, int(b.get('days', 5)))),
                           threshold=max(0.01, min(20, float(b.get('threshold', 0.3)))),
                           ma_n=max(2, min(120, int(b.get('ma_n', 20)))),
                           min_volume=float(b.get('min_volume', 0) or 0),
                           include_etf=bool(b.get('include_etf')), asof=b.get('asof') or None)
    except (TypeError, ValueError) as e:
        return jsonify(error=f'參數錯誤：{e}'), 400
    if res is None:
        return jsonify(error='歷史資料尚未載入完成'), 503
    return jsonify(res)


@bp.route('/api/tw/board/scorecard')
def api_scorecard():
    res = get_scorecard()
    if res is None:
        return jsonify(error='歷史資料不足（至少需要 120 個交易日）'), 503
    return jsonify(res)


@bp.route('/api/tw/board/alerts', methods=['GET', 'POST'])
def api_alerts():
    user = userdata.current()
    with _alert_lock:
        cfg = _load_alerts(user)
        if request.method == 'POST':
            b = request.get_json(silent=True) or {}
            cfg['enabled'] = bool(b.get('enabled'))
            cfg['scope'] = str(b.get('scope') or 'watch')
            cfg['rules'] = [r for r in b.get('rules', []) if r in RT_RULES] or list(RT_RULES)
            cfg['params'] = {k: float(v) for k, v in (b.get('params') or {}).items()
                             if k in ('kd_n', 'k_low', 'k_high', 'ma_band')}
            cfg['include_etf'] = bool(b.get('include_etf'))
            _save_alerts(user, cfg)
    token, uid = _line_creds(user)
    out = {k: v for k, v in cfg.items() if k != 'sent'}
    out['line_ready'] = bool(token and uid)
    return jsonify(out)


@bp.route('/api/tw/board/alerts/test', methods=['POST'])
def api_alerts_test():
    ok, err = _push_line(f'【StockLens】LINE 推播測試成功 {_now():%Y-%m-%d %H:%M}', userdata.current())
    return (jsonify(ok=True), 200) if ok else (jsonify(error=err), 400)


@bp.route('/api/tw/board/screen/realtime', methods=['GET', 'POST'])
def api_screen_realtime():
    _touch_active()
    if request.method == 'GET':
        return jsonify([dict(id=k, label=v['label'], side=v['side']) for k, v in RT_RULES.items()])
    b = request.get_json(silent=True) or {}
    params = dict(kd_n=b.get('kd_n', 9),
                  k_low=float(b.get('k_low', 20)), k_high=float(b.get('k_high', 80)),
                  ma_band=max(0.05, min(20, float(b.get('ma_band', 1)))))
    res = screen_realtime(b.get('rules') or list(RT_RULES), params,
                          include_etf=bool(b.get('include_etf')))
    if res is None:
        return jsonify(error='歷史資料尚未載入完成，請稍後再試'), 503
    res['market_open'] = _market_open()
    return jsonify(res)


def init(app, login_required):
    _init_db()

    @app.route('/tw/board')
    @login_required
    def tw_board_page():
        return render_template('tw_board.html')

    @app.route('/tw/board/chart')
    @login_required
    def tw_board_chart_page():
        return render_template('tw_chart.html')

    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_sync_loop, daemon=True).start()
        threading.Thread(target=_rt_loop, daemon=True).start()
