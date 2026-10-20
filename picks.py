"""潛力股：多因子評分 + 歷史回測（台股 / 美股）。

做法（和法人量化研究一樣的流程）：
1. 每個交易日，把流動性足夠的股票依各個因子排名（百分位 0～1）。
2. 綜合分數 = 各因子排名的等權平均（反向因子用 1 - 排名），× 100。
   權重刻意不最佳化，避免把過去的雜訊當成規律。
3. 因子與方向是用「前段」資料挑的，再用「後段」資料驗證；
   頁面上同時列出每個因子在兩段的 IC（排名與之後報酬的相關係數），兩段都為正才算穩定。
4. 回測：每天依分數分成 10 組看之後 N 日的超額報酬（減掉同日全體平均），
   以及「每 N 天換一次、持有分數最高 20 檔」扣掉交易成本後和大盤的累積報酬。

台股資料：證交所／櫃買中心官方日 K、三大法人、公開資訊觀測站月營收（tw_board 的資料庫）。
美股資料：S&P 500 + Nasdaq-100 成分股，yfinance 還原日 K，存在 us_market.db。
美股用的是「目前」成分股回測，會有倖存者偏差（已被剔除的弱勢股不在樣本內），頁面上會註明。
"""
import os
import re
import html
import json
import time
import fcntl
import sqlite3
import threading
import warnings

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from flask import Blueprint, jsonify, request, render_template

import tw_board

bp = Blueprint('picks', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
US_DB_PATH = os.path.join(BASE_DIR, 'us_market.db')
CACHE_FILE = os.path.join(BASE_DIR, '.picks_{}.json')
LOCK_FILE = os.path.join(BASE_DIR, '.picks_sync.lock')

TOP_N = 20
HORIZONS = (5, 10, 20)
COST = {'tw': 0.45, 'us': 0.10}          # 每次換股的來回成本（%）：台股手續費 + 證交稅；美股抓價差
MIN_LIQ = {'tw': 500, 'us': 2e7}         # 20 日均量 ≥ 500 張；美股 20 日均成交額 ≥ 2,000 萬美元
REV_MONTHS = 34                          # 月營收保留月數（3 個月年增需要 3 個月、回測約兩年）
HDRS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockLens'}


# ── 台股月營收（公開資訊觀測站）────────────────────────────────────────
def _init_tw_tables():
    with tw_board._db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS revenue(code TEXT, ym TEXT, rev REAL, rev_ly REAL,
            PRIMARY KEY(code, ym));   -- 千元；ym = YYYY-MM
        CREATE TABLE IF NOT EXISTS valuation(code TEXT PRIMARY KEY, date TEXT, pe REAL, pb REAL, dy REAL);
        ''')


def _parse_revenue(raw):
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', raw, re.S):
        tds = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip().replace(',', '')
               for x in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if len(tds) >= 5 and re.fullmatch(r'\d{4}', tds[0]):
            rev, ly = tw_board._num(tds[2]), tw_board._num(tds[4])   # 當月營收、去年當月營收
            if rev is not None:
                rows.append((tds[0], rev, ly))
    return rows


def _months_back(n):
    p = pd.Period(tw_board._now().strftime('%Y-%m'), 'M')
    return [str(p - i) for i in range(1, n + 1)]


def sync_revenue():
    """補齊最近 REV_MONTHS 個月的月營收；最近兩個月每天重抓（公司在次月 10 日前陸續公布）。"""
    _init_tw_tables()
    months = _months_back(REV_MONTHS)
    with tw_board._db() as con:
        have = dict(con.execute('SELECT ym, COUNT(*) FROM revenue GROUP BY ym'))
    done = 0
    for i, ym in enumerate(months):
        if have.get(ym, 0) > 1200 and i >= 2:
            continue
        y, m = map(int, ym.split('-'))
        rows = []
        for mk in ('sii', 'otc'):
            url = f'https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{y - 1911}_{m}_0.html'
            try:
                r = requests.get(url, headers=HDRS, timeout=60)
                if r.status_code == 200:
                    rows += _parse_revenue(r.content.decode('big5', 'ignore'))
            except Exception as e:
                print(f'[picks] revenue {ym} {mk}: {e}')
            time.sleep(0.8)
        if rows:
            with tw_board._db() as con:
                con.executemany('INSERT OR REPLACE INTO revenue VALUES(?,?,?,?)',
                                [(c, ym, rev, ly) for c, rev, ly in rows])
            done += 1
    with tw_board._db() as con:
        con.execute('DELETE FROM revenue WHERE ym < ?', (months[-1],))
    return done


def sync_valuation():
    """本益比、股價淨值比、殖利率（只用來顯示與提示風險，沒有歷史所以不進分數）。"""
    _init_tw_tables()
    rows = {}
    try:
        for r in tw_board._get_json('https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL', host_gap=1):
            rows[r.get('Code', '').strip()] = (r.get('Date'), tw_board._num(r.get('PEratio')),
                                              tw_board._num(r.get('PBratio')), tw_board._num(r.get('DividendYield')))
    except Exception as e:
        print(f'[picks] valuation twse: {e}')
    try:
        for r in tw_board._get_json('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis', host_gap=1):
            rows[r.get('SecuritiesCompanyCode', '').strip()] = (
                r.get('Date'), tw_board._num(r.get('PriceEarningRatio')),
                tw_board._num(r.get('PriceBookRatio')), tw_board._num(r.get('YieldRatio')))
    except Exception as e:
        print(f'[picks] valuation tpex: {e}')
    if rows:
        with tw_board._db() as con:
            con.executemany('INSERT OR REPLACE INTO valuation VALUES(?,?,?,?,?)',
                            [(c,) + v for c, v in rows.items() if c])


def _revenue_frame(dates, cols):
    """近 3 個月營收年增率（%），對齊到交易日。月營收以次月 11 日起才算已公布，避免回測偷看。"""
    with tw_board._db() as con:
        df = pd.read_sql_query('SELECT code, ym, rev, rev_ly FROM revenue', con)
    if df.empty:
        return None, None
    rev = df.pivot(index='ym', columns='code', values='rev').sort_index()
    ly = df.pivot(index='ym', columns='code', values='rev_ly').reindex_like(rev)
    yoy3 = (rev.rolling(3).sum() / ly.where(ly > 0).rolling(3).sum() - 1) * 100
    yoy1 = (rev / ly.where(ly > 0) - 1) * 100
    avail = [(pd.Period(ym, 'M') + 1).to_timestamp() + pd.Timedelta(days=10) for ym in rev.index]
    idx = pd.DatetimeIndex(pd.to_datetime(dates))

    def align(x):
        x = x.copy()
        x.index = pd.DatetimeIndex(avail)
        out = x.reindex(x.index.union(idx)).ffill(limit=None).reindex(idx)
        out.index = dates
        return out.reindex(columns=cols)
    return align(yoy3), dict(yoy1=align(yoy1), ym=align(pd.DataFrame(
        {c: rev.index for c in rev.columns}, index=rev.index).where(rev.notna())))


# ── 美股資料（S&P 500 + Nasdaq-100）──────────────────────────────────────
def _us_db():
    con = sqlite3.connect(US_DB_PATH, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    return con


def _init_us_db():
    with _us_db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS us_stocks(code TEXT PRIMARY KEY, name TEXT, sector TEXT, idx TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS us_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS us_sync(key TEXT PRIMARY KEY, value TEXT);
        ''')


def _us_universe():
    out = {}
    raw = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies', headers=HDRS, timeout=30).text
    tbl = re.search(r'<table[^>]*id="constituents".*?</table>', raw, re.S)
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', tbl.group(0) if tbl else '', re.S)[1:]:
        tds = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip() for x in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if len(tds) >= 3:
            out[tds[0].replace('.', '-')] = dict(name=tds[1], sector=tds[2], idx='S&P 500')
    js = requests.get('https://api.nasdaq.com/api/quote/list-type/nasdaq100',
                      headers=dict(HDRS, Accept='application/json'), timeout=30).json()
    for r in js['data']['data']['rows']:
        code = r['symbol'].replace('.', '-')
        name = re.sub(r'\s+(Class [A-C] )?(Common Stock|Ordinary Shares|American Depositary Shares).*$', '',
                      r.get('companyName', '')).strip()
        if code in out:
            out[code]['idx'] = 'S&P 500・Nasdaq-100'
        else:
            out[code] = dict(name=name, sector=r.get('sector') or '', idx='Nasdaq-100')
    return out


def sync_us(force=False):
    """每天美股收盤後（台北時間 06:00 之後）重抓 3 年還原日 K；成分股每週更新。"""
    _init_us_db()
    now = tw_board._now()
    with _us_db() as con:
        last = dict(con.execute('SELECT key, value FROM us_sync'))
        n = con.execute('SELECT COUNT(*) FROM us_stocks').fetchone()[0]
    stamp = (now - pd.Timedelta(hours=6)).strftime('%Y-%m-%d')    # 台北 06:00 前算前一天
    if not force and last.get('daily') == stamp and n:
        return False
    if force or not n or last.get('universe', '') < (now - pd.Timedelta(days=7)).strftime('%Y-%m-%d'):
        try:
            uni = _us_universe()
            if len(uni) > 400:
                with _us_db() as con:
                    con.execute('DELETE FROM us_stocks')
                    con.executemany('INSERT INTO us_stocks VALUES(?,?,?,?,?)',
                                    [(c, v['name'], v['sector'], v['idx'], now.strftime('%Y-%m-%d')) for c, v in uni.items()])
                    con.execute("INSERT OR REPLACE INTO us_sync VALUES('universe', ?)", (now.strftime('%Y-%m-%d'),))
        except Exception as e:
            print(f'[picks] us universe: {e}')
    with _us_db() as con:
        codes = [r[0] for r in con.execute('SELECT code FROM us_stocks ORDER BY code')]
    if not codes:
        return False
    d = yf.download(codes, period='3y', interval='1d', auto_adjust=True, group_by='column',
                    threads=True, progress=False)
    if d is None or d.empty:
        return False
    rows = []
    for c in codes:
        try:
            sub = pd.DataFrame({k: d[k][c] for k in ('Open', 'High', 'Low', 'Close', 'Volume')}).dropna(subset=['Close'])
        except KeyError:
            continue
        rows += [(c, ts.strftime('%Y-%m-%d'), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
                 for ts, r in sub.iterrows()]
    if len(rows) < 100000:
        print(f'[picks] us download too small: {len(rows)} rows')
        return False
    with _us_db() as con:
        con.execute('DELETE FROM us_daily')        # 還原價會因除息整段改變，整批換掉
        con.executemany('INSERT INTO us_daily VALUES(?,?,?,?,?,?,?)', rows)
        con.execute("INSERT OR REPLACE INTO us_sync VALUES('daily', ?)", (stamp,))
    return True


def _us_frames():
    _init_us_db()
    with _us_db() as con:
        px = pd.read_sql_query('SELECT * FROM us_daily', con)
        meta = pd.read_sql_query('SELECT * FROM us_stocks', con).set_index('code')
    if px.empty:
        return None
    piv = lambda col: px.pivot(index='date', columns='code', values=col).sort_index()
    C = piv('close')
    return dict(close=C, high=piv('high').reindex_like(C), low=piv('low').reindex_like(C),
                volume=piv('volume').reindex_like(C).fillna(0), meta=meta, dates=list(C.index))


# ── 因子 ──────────────────────────────────────────────────────────────
# (id, 名稱, 說明, 方向) — 方向依前段資料的 IC 決定，後段驗證；頁面會列出兩段 IC
TW_FACTORS = [
    ('foreign', '外資買超', '近 20 日外資買超張數佔發行股數的比例', 1),
    ('breakout', '突破強度', '收盤價相對近 20 日最高價，越接近或創新高分數越高', 1),
    ('trend', '月線位置', '收盤價高於 20 日均線的幅度', 1),
    ('revenue', '營收成長', '近 3 個月營收年增率（次月 11 日起才採用）', 1),
    ('trust_hot', '投信不擁擠', '近 20 日投信買超佔發行股數越低分數越高：投信集中大買後常見回落（反向指標）', -1),
]
US_FACTORS = [
    ('mom12', '12 個月動能', '過去 12 個月報酬，不含最近 1 個月（學術上最穩定的動能因子）', 1),
    ('trend200', '長期趨勢', '200 日均線近 1 個月的斜率', 1),
    ('pullback', '強勢回檔', '離 20 日高點越遠分數越高：美股短線有均值回歸，強勢股拉回時較有利', -1),
]


def _rsi(C, n=14):
    d = C.diff()
    up, dn = d.clip(lower=0).rolling(n).mean(), (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _tw_inputs():
    f = tw_board._load_frames(tw_board.BACKFILL_DAYS + 10)
    if f is None or len(f['dates']) < 160:
        return None
    meta = f['meta']
    cols = [c for c in f['close'].columns if not c.startswith('00')
            and (c not in meta.index or meta.loc[c, 'kind'] == 'stock')]
    C, H, L, V = (f[k][cols] for k in ('close', 'high', 'low', 'volume'))
    iss = f['issued'].reindex(cols)
    FN, TN = f['foreign_net'][cols], f['trust_net'][cols]
    ma20 = C.rolling(20).mean()
    foreign20 = FN.rolling(20).sum().div(iss, axis=1) * 100
    trust20 = TN.rolling(20).sum().div(iss, axis=1) * 100
    rev, rev_extra = _revenue_frame(f['dates'], cols)
    X = dict(foreign=foreign20, breakout=C / H.rolling(20).max(), trend=C / ma20 - 1,
             high120=C / H.rolling(120).max(), trust_hot=trust20)
    factors = [x for x in TW_FACTORS if x[0] != 'revenue' or rev is not None]
    if rev is not None:
        X['revenue'] = rev
    liq = V.rolling(20).mean() >= MIN_LIQ['tw']
    with tw_board._db() as con:
        val = {r[0]: r[1:] for r in con.execute('SELECT code, pe, pb, dy FROM valuation')}
    trust_days = (TN > 0).astype(int)
    trust_streak = trust_days.iloc[::-1].cumprod().sum()          # 投信連續買超天數（到今天）
    extra = dict(foreign20=foreign20, trust20=trust20, rsi=_rsi(C), ma20=ma20, val=val, trust_streak=trust_streak,
                 rev_yoy1=rev_extra['yoy1'] if rev_extra else None, rev_ym=rev_extra['ym'] if rev_extra else None,
                 chg=f['chg'][cols])
    return dict(market='tw', C=C, H=H, L=L, V=V, liq=liq, X=X, factors=factors, meta=meta,
                dates=f['dates'], extra=extra, warm=125)


def _us_inputs():
    f = _us_frames()
    if f is None or len(f['dates']) < 400:
        return None
    C, H, L, V = f['close'], f['high'], f['low'], f['volume']
    ma200 = C.rolling(200).mean()
    X = dict(mom12=C.shift(21) / C.shift(252) - 1, trend200=ma200 / ma200.shift(21) - 1,
             pullback=C / H.rolling(20).max())
    liq = (C * V).rolling(20).mean() >= MIN_LIQ['us']
    ret = C.pct_change(fill_method=None)
    extra = dict(rsi=_rsi(C), ma50=C.rolling(50).mean(), ma200=ma200,
                 vol60=ret.rolling(60).std() * np.sqrt(252) * 100, chg=C.diff())
    return dict(market='us', C=C, H=H, L=L, V=V, liq=liq, X=X, factors=US_FACTORS, meta=f['meta'],
                dates=f['dates'], extra=extra, warm=260)


# ── 評分與回測引擎 ─────────────────────────────────────────────────────
def _row_ic(x, y):
    """每天的橫斷面相關係數（兩者都已是排名）。"""
    xm = x.sub(x.mean(1), axis=0)
    ym = y.where(x.notna()).sub(y.where(x.notna()).mean(1), axis=0)
    xm = xm.where(ym.notna())
    return (xm * ym).sum(1) / np.sqrt((xm ** 2).sum(1) * (ym ** 2).sum(1))


def build(market):
    inp = _tw_inputs() if market == 'tw' else _us_inputs()
    if inp is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return _build(inp)


def _build(inp):
    C, H, L, liq, X = inp['C'], inp['H'], inp['L'], inp['liq'], inp['X']
    market, dates, warm = inp['market'], inp['dates'], inp['warm']
    n = len(C)
    half = (warm + n) // 2
    periods = {'first': (warm, half), 'second': (half, n), 'all': (warm, n)}

    ranks = {}
    for fid, _, _, sign in inp['factors']:
        r = X[fid].where(liq).rank(axis=1, pct=True)
        ranks[fid] = r if sign > 0 else 1 - r
    stack = np.stack([ranks[f].values for f, *_ in inp['factors']])
    cnt = np.sum(~np.isnan(stack), axis=0)
    score = np.where(cnt >= max(2, len(inp['factors']) // 2), np.nanmean(stack, axis=0) * 100, np.nan)
    score = pd.DataFrame(score, index=C.index, columns=C.columns).where(liq)
    srank = score.rank(axis=1, pct=True)
    decile = np.ceil(srank * 10).clip(1, 10)
    top = score.rank(axis=1, ascending=False, method='first') <= TOP_N

    out = dict(market=market, start=dates[warm], split=dates[half], end=dates[-1], days=n - warm,
               top_n=TOP_N, cost=COST[market], min_liq=MIN_LIQ[market], horizons={}, factors=[])
    for h in HORIZONS:
        fwd = (C.shift(-h) / C - 1).where(liq) * 100
        ex = fwd.sub(fwd.mean(1), axis=0)
        res = dict(deciles=[], top={}, market={})
        for p, (a, b) in periods.items():
            res['market'][p] = dict(avg=round(float(fwd.iloc[a:b].mean(1).mean()), 2),
                                    win=round(float((fwd.iloc[a:b] > 0).sum().sum() / fwd.iloc[a:b].notna().sum().sum() * 100), 1))
            t = top.iloc[a:b]
            r, x = fwd.iloc[a:b].where(t), ex.iloc[a:b].where(t)
            k = r.notna().sum().sum()
            res['top'][p] = None if not k else dict(
                avg=round(float(r.mean(1).mean()), 2), excess=round(float(x.mean(1).mean()), 2),
                win=round(float((r > 0).sum().sum() / k * 100), 1), beat=round(float((x > 0).sum().sum() / k * 100), 1))
        for d in range(1, 11):
            m = decile == d
            row = dict(d=d)
            for p, (a, b) in periods.items():
                x = ex.iloc[a:b].where(m.iloc[a:b])
                k = x.notna().sum().sum()
                row[p] = round(float(x.mean(1).mean()), 2) if k else None
                if p == 'all' and k:
                    r = fwd.iloc[a:b].where(m.iloc[a:b])
                    row['beat'] = round(float((x > 0).sum().sum() / k * 100), 1)
                    row['win'] = round(float((r > 0).sum().sum() / k * 100), 1)
            res['deciles'].append(row)
        # 每 h 天換股：前 20 名 vs 全體等權，扣成本
        curve, s_eq, m_eq = [], 1.0, 1.0
        for i in range(warm, n - h, h):
            sel = top.iloc[i] & fwd.iloc[i].notna()
            if not sel.any():
                continue
            s_eq *= 1 + (fwd.iloc[i][sel].mean() - COST[market]) / 100
            m_eq *= 1 + fwd.iloc[i].mean() / 100
            curve.append([dates[i + h], round((s_eq - 1) * 100, 2), round((m_eq - 1) * 100, 2)])
        res['curve'] = [[dates[warm], 0, 0]] + curve
        out['horizons'][h] = res

    # 因子 IC（20 日）
    fwd20r = (C.shift(-20) / C - 1).where(liq).rank(axis=1, pct=True)
    ics = {fid: _row_ic(ranks[fid], fwd20r) for fid, *_ in inp['factors']}
    ics['_score'] = _row_ic(srank, fwd20r)
    for fid, name, desc, sign in inp['factors'] + [('_score', '綜合分數', '以上因子等權平均', 1)]:
        s = ics[fid]
        a = round(float(s.iloc[warm:half].mean()), 4)
        b = round(float(s.iloc[half:n - 20].mean()), 4)
        out['factors'].append(dict(id=fid, name=name, desc=desc, sign=sign, ic_first=a, ic_second=b,
                                   stable=a > 0 and b > 0))

    # 今日名單（全部流動性足夠的股票，前端再篩選）
    exp = {r['d']: (r['all'], r.get('beat'), r.get('win')) for r in out['horizons'][20]['deciles']}
    last = score.iloc[-1].dropna().sort_values(ascending=False)
    atr = (pd.concat([H - L, (H - C.shift()).abs(), (L - C.shift()).abs()]).groupby(level=0).max()
           .reindex(C.index).rolling(14).mean())
    meta, ex_ = inp['meta'], inp['extra']
    rows = []
    for rank, (code, sc) in enumerate(last.items(), 1):
        c = float(C[code].iloc[-1])
        chg = ex_['chg'][code].iloc[-1]
        prev = c - chg if pd.notna(chg) else None
        d = int(decile[code].iloc[-1])
        m = meta.loc[code] if code in meta.index else None
        a = atr[code].iloc[-1]
        row = dict(code=code, rank=rank, score=round(float(sc), 1), decile=d,
                   name=(m['name'] if m is not None else code),
                   group=(m['industry'] if market == 'tw' else m['sector']) if m is not None else '',
                   close=round(c, 2), chg_pct=round(float(chg / prev * 100), 2) if prev else None,
                   f={fid: (None if pd.isna(ranks[fid][code].iloc[-1]) else round(float(ranks[fid][code].iloc[-1]) * 100))
                      for fid, *_ in inp['factors']},
                   exp20=exp[d][0], beat20=exp[d][1], win20=exp[d][2],
                   stop=round(c - 2 * float(a), 2) if pd.notna(a) else None)
        row.update(_explain(market, code, inp, row))
        rows.append(row)
    out['stocks'] = rows
    out['universe'] = int(liq.iloc[-1].sum())
    groups = pd.Series([r['group'] or '其他' for r in rows[:TOP_N]]).value_counts()
    out['top_groups'] = [[g, int(k)] for g, k in groups.items()]
    return out


def _v(df, code):
    if df is None or code not in df.columns:
        return None
    v = df[code].iloc[-1]
    return None if pd.isna(v) else float(v)


def _explain(market, code, inp, row):
    """把數字翻成分析師會寫的理由與風險。"""
    X, ex, C, H = inp['X'], inp['extra'], inp['C'], inp['H']
    why, risk, info = [], [], {}
    rsi = _v(ex['rsi'], code)
    if market == 'tw':
        fr, tr = _v(ex['foreign20'], code), _v(ex['trust20'], code)
        br, hi120, tdev = _v(X['breakout'], code), _v(X['high120'], code), _v(X['trend'], code)
        yoy3 = _v(X.get('revenue'), code)
        yoy1 = _v(ex.get('rev_yoy1'), code)
        ym = ex['rev_ym'][code].iloc[-1] if ex.get('rev_ym') is not None and code in ex['rev_ym'].columns else None
        streak = int(ex['trust_streak'].get(code, 0))
        if fr is not None and fr >= 0.3:
            why.append(f'外資 20 日買超 {fr:.2f}% 股本')
        elif fr is not None and fr <= -0.5:
            risk.append(f'外資 20 日賣超 {abs(fr):.2f}% 股本')
        if br is not None and br >= 0.995:
            why.append('創 20 日新高')
        elif br is not None and br >= 0.97:
            why.append(f'距 20 日高點 {(1 - br) * 100:.1f}%')
        if hi120 is not None and hi120 >= 0.995:
            why.append('創半年新高')
        if tdev is not None and tdev > 0:
            why.append(f'站上月線 +{tdev * 100:.1f}%')
        if tdev is not None and tdev >= 0.15:
            risk.append(f'乖離月線 {tdev * 100:.0f}%，追高風險')
        if yoy3 is not None and yoy3 >= 300:
            why.append('近 3 月營收年增 >300%（去年基期低）')
        elif yoy3 is not None and yoy3 >= 15:
            why.append(f'近 3 月營收年增 {yoy3:.0f}%')
        elif yoy3 is not None and yoy3 <= -15:
            risk.append(f'近 3 月營收年減 {abs(yoy3):.0f}%')
        if tr is not None and tr >= 2:
            risk.append(f'投信 20 日買超 {tr:.1f}% 股本，籌碼擁擠')
        elif streak >= 5:
            why.append(f'投信連買 {streak} 天')
        pe, pb, dy = (inp['extra']['val'].get(code) or (None, None, None))
        if pe is None and pb is not None:
            risk.append('近四季虧損（無本益比）')
        elif pe is not None and pe >= 60:
            risk.append(f'本益比 {pe:.0f} 倍偏高')
        info.update(foreign20=fr, trust20=tr, rev_yoy3=yoy3, rev_yoy1=yoy1,
                    rev_ym=ym if isinstance(ym, str) else None, pe=pe, pb=pb, dy=dy)
    else:
        m12, slope, pb_ = _v(X['mom12'], code), _v(X['trend200'], code), _v(X['pullback'], code)
        ma50, ma200, vol = _v(ex['ma50'], code), _v(ex['ma200'], code), _v(ex['vol60'], code)
        c = float(C[code].iloc[-1])
        if m12 is not None and m12 >= 0.2:
            why.append(f'12 個月動能 +{m12 * 100:.0f}%')
        elif m12 is not None and m12 < 0:
            risk.append(f'12 個月動能 {m12 * 100:.0f}%')
        if slope is not None and slope > 0:
            why.append('200 日均線上揚')
        elif slope is not None and slope < 0:
            risk.append('200 日均線下彎')
        if ma200 and c > ma200:
            why.append(f'股價在 200 日線之上 +{(c / ma200 - 1) * 100:.0f}%')
        if pb_ is not None and pb_ <= 0.95 and (slope or 0) > 0:
            why.append(f'強勢股回檔 {(1 - pb_) * 100:.0f}%')
        if ma50 and c / ma50 - 1 >= 0.2:
            risk.append(f'高於 50 日線 {(c / ma50 - 1) * 100:.0f}%，短線過熱')
        if vol is not None and vol >= 60:
            risk.append(f'年化波動 {vol:.0f}%，部位宜小')
        info.update(mom12=None if m12 is None else round(m12 * 100, 1), vol60=None if vol is None else round(vol, 1))
    if rsi is not None and rsi >= 80:
        risk.append(f'RSI {rsi:.0f} 過熱')
    info = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in info.items()}
    return dict(why=why, risk=risk, info=info)


# ── 快取與背景更新 ─────────────────────────────────────────────────────
def _version(market):
    if market == 'tw':
        with tw_board._db() as con:
            rv = con.execute("SELECT MAX(ym), COUNT(*) FROM revenue").fetchone() if _has_table(con, 'revenue') else None
            vl = con.execute("SELECT MAX(date) FROM valuation").fetchone() if _has_table(con, 'valuation') else None
        return [list(tw_board._data_version()), list(rv or []), list(vl or [])]
    _init_us_db()
    with _us_db() as con:
        return [list(con.execute("SELECT value FROM us_sync WHERE key='daily'").fetchone() or [])]


def _has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


_build_lock = threading.Lock()


def get_picks(market):
    """讀快取（資料版本相同就沿用），否則重算並寫回檔案供其他 worker 共用。"""
    ver = _version(market)
    path = CACHE_FILE.format(market)
    try:
        with open(path, encoding='utf-8') as fh:
            cached = json.load(fh)
        if cached.get('version') == ver:
            return cached
    except (OSError, ValueError):
        cached = None
    with _build_lock:
        res = build(market)
    if res is None:
        return cached
    res['version'] = ver
    res['built'] = tw_board._now().strftime('%Y-%m-%d %H:%M')
    with open(path + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(_clean(res), fh, ensure_ascii=False, allow_nan=False)
    os.replace(path + '.tmp', path)
    return res


def _clean(o):
    """NaN / numpy 數值 → JSON 可用的值。"""
    if isinstance(o, dict):
        return {(int(k) if isinstance(k, np.integer) else k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if np.isnan(o) or np.isinf(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def _sync_loop():
    """只有一個 worker 會拿到鎖：每 30 分鐘檢查一次，有新資料就更新並預先算好。"""
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    time.sleep(90)              # 讓 tw_board 先完成啟動同步
    while True:
        day = tw_board._now().strftime('%Y-%m-%d')
        for name, fn in (('valuation', sync_valuation), ('revenue', sync_revenue), ('us', sync_us)):
            try:
                if name == 'us' or _status.get(name) != day:
                    fn()
                    _status[name] = day
            except Exception as e:
                print(f'[picks] sync {name}: {e}')
        for m in ('tw', 'us'):
            try:
                get_picks(m)
            except Exception as e:
                print(f'[picks] build {m}: {e}')
        time.sleep(1800)


_status = {}


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/picks/<market>')
def api_picks(market):
    if market not in ('tw', 'us'):
        return jsonify(error='market 必須是 tw 或 us'), 400
    res = get_picks(market)
    if not res:
        return jsonify(error='資料尚未準備好（第一次啟動需要幾分鐘下載歷史資料），請稍後再試'), 503
    return jsonify(res)


@bp.route('/api/picks/<market>/stock/<code>')
def api_pick_stock(market, code):
    if market not in ('tw', 'us'):
        return jsonify(error='market 必須是 tw 或 us'), 400
    res = get_picks(market)
    if not res:
        return jsonify(error='資料尚未準備好'), 503
    code = code.upper().replace('.TWO', '').replace('.TW', '').replace('.', '-')
    row = next((r for r in res['stocks'] if r['code'] == code), None)
    if not row:
        return jsonify(error='不在評分範圍內（流動性不足、資料不足或不在股票池）', universe=res['universe']), 404
    return jsonify(dict(row, universe=res['universe'], end=res['end'], factors=res['factors']))


def init(app, login_required):
    _init_tw_tables()
    _init_us_db()

    @app.route('/picks')
    @login_required
    def picks_page():
        return render_template('picks.html')

    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_sync_loop, daemon=True).start()
