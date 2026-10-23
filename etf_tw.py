"""台股 ETF 分析：折溢價、規模與資金流向、殖利率與填息、成分股重疊度、合併曝險、配息日曆。

資料來源：
- 證交所 ETF 即時淨值（mis.twse.com.tw/stock/data/all_etf.txt）：上市、上櫃 ETF 的市價、預估淨值、
  折溢價、已發行受益單位數與較前一日增減（＝申購／贖回的資金流向）。
- MoneyDJ 的 ETF 全部持股：每日更新的完整成分股與比例。
- Yahoo Finance：歷史配息；本地資料庫：每日收盤價（算含息報酬與填息天數）。
- 證交所、櫃買中心的除權息預告表：即將除息的 ETF 與配息金額。
"""
import os
import re
import json
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Blueprint, jsonify, request, render_template

import financials
import tw_board

bp = Blueprint('etf_tw', __name__)

HDRS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockLens'}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.etf_cache')
_mem, _lock = {}, threading.Lock()
POPULAR = ['0050', '006208', '0056', '00878', '00919', '00929', '00940', '00713', '00692', '00679B']


def _cached(key, ttl, fetch):
    with _lock:
        hit = _mem.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fetch()
    with _lock:
        _mem[key] = (time.time(), val)
    return val


def _num(x):
    try:
        return float(str(x).replace(',', ''))
    except (TypeError, ValueError):
        return None


def _roc(s):
    """'115年11月06日' 或 '115/10/19' → '2026-11-06'。"""
    m = re.match(r'(\d{2,3})\D(\d{1,2})\D(\d{1,2})', s or '')
    return f'{int(m.group(1)) + 1911}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else None


# ── 即時淨值與折溢價 ──────────────────────────────────────────────────
def all_etf():
    def fetch():
        js = requests.get('https://mis.twse.com.tw/stock/data/all_etf.txt', headers=HDRS, timeout=15).json()
        out = {}
        for g in js.get('a1', []):
            for m in g.get('msgArray', []):
                code = m.get('a')
                price, nav, units = _num(m.get('e')), _num(m.get('f')), _num(m.get('c'))
                if not code:
                    continue
                name = re.sub(r'\(.*?\)$', '', m.get('b') or '').strip()
                out[code] = {'code': code, 'name': name, 'price': price, 'nav': nav, 'premium': _num(m.get('g')),
                             'prev_nav': _num(m.get('h')), 'units': units, 'units_chg': _num(m.get('d')),
                             'size': round(units * nav / 1e8, 1) if units and nav else None,       # 億元
                             'date': m.get('i'), 'time': m.get('j')}
        return out
    tw_open = tw_board._market_open()
    return _cached('all_etf', 60 if tw_open else 600, fetch)


# ── 成分股 ────────────────────────────────────────────────────────────
def holdings(code):
    def fetch():
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, f'h_{code}.json')
        try:
            with open(path, encoding='utf-8') as f:
                disk = json.load(f)
            if time.time() - disk['ts'] < 12 * 3600:
                return disk['data']
        except (OSError, ValueError, KeyError):
            disk = None
        suffix = 'TWO' if _market(code) == 'OTC' else 'TW'
        try:
            raw = requests.get(f'https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={code}.{suffix}',
                               headers=HDRS, timeout=20).content.decode('utf-8', 'ignore')     # 網頁沒標編碼，requests 會誤判
        except requests.RequestException:
            return disk['data'] if disk else None
        date = re.search(r'sdate3"[^>]*>資料日期：([\d/]+)', raw)
        tbl = re.search(r'id="[^"]*stable3".*?</table>', raw, re.S)
        rows = []
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', tbl.group(0) if tbl else '', re.S):
            tds = [re.sub(r'<[^>]+>', '', x).strip() for x in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
            if len(tds) < 2:
                continue
            m = re.match(r'(.*?)\(([\w.]+)\)$', tds[0])
            name, sym = (m.group(1), m.group(2)) if m else (tds[0], '')
            pct = _num(tds[1])
            if pct is None:
                continue
            c = re.sub(r'\.(TW|TWO)$', '', sym)
            rows.append({'name': name, 'code': c if re.fullmatch(r'\d{4,6}[A-Z]?', c) else '', 'pct': pct,
                         'shares': _num(tds[2]) if len(tds) > 2 else None})
        data = {'date': date.group(1).replace('/', '-') if date else None, 'rows': rows}
        if rows:
            with open(path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'data': data}, f, ensure_ascii=False)
            os.replace(path + '.tmp', path)
        return data if rows else (disk['data'] if disk else data)
    return _cached(f'h:{code}', 3600, fetch)


def _market(code):
    with tw_board._db() as con:
        r = con.execute('SELECT market FROM stocks WHERE code=?', (code,)).fetchone()
    return r[0] if r else 'TSE'


def _industries(codes):
    if not codes:
        return {}
    with tw_board._db() as con:
        q = f"SELECT code, industry FROM stocks WHERE code IN ({','.join('?' * len(codes))})"
        return dict(con.execute(q, codes).fetchall())


# ── 配息、報酬、填息 ──────────────────────────────────────────────────
def _closes(code, days=800):
    since = (pd.Timestamp.now() - pd.Timedelta(days=days)).strftime('%Y-%m-%d')
    with tw_board._db() as con:
        df = pd.read_sql_query('SELECT date, close FROM daily WHERE code=? AND date>=? ORDER BY date', con, params=(code, since))
    return pd.Series(df['close'].values, index=pd.to_datetime(df['date'])) if len(df) else pd.Series(dtype=float)


def _dividends(code):
    suffix = 'TWO' if _market(code) == 'OTC' else 'TW'
    t = financials.CachedTicker(f'{code}.{suffix}')
    try:
        d = t.dividends
    except Exception:
        d = None
    finally:
        t.save()
    if d is None or len(d) == 0:
        return pd.Series(dtype=float)
    d.index = pd.to_datetime(d.index.tz_localize(None) if d.index.tz is not None else d.index).normalize()
    return d


def dividend_stats(code, price):
    d, px = _dividends(code), _closes(code)
    now = pd.Timestamp.now().normalize()
    last12 = d[d.index > now - pd.Timedelta(days=365)]
    per_year = len(d[d.index > now - pd.Timedelta(days=730)]) / 2 if len(d) else 0
    freq = {0: '不配息', 1: '年配', 2: '半年配', 4: '季配', 12: '月配'}.get(int(round(per_year)), f'一年約 {per_year:.0f} 次') if len(d) else '無配息紀錄'
    events = []
    for dt, amt in list(d.items())[-8:]:
        before = px[px.index < dt]
        after = px[px.index >= dt]
        if before.empty or after.empty:
            events.append({'date': str(dt.date()), 'amount': round(float(amt), 4), 'yield': None, 'fill_days': None})
            continue
        ref = float(before.iloc[-1])
        hit = after[after >= ref]
        fill = None if hit.empty else int((after.index <= hit.index[0]).sum()) - 1
        events.append({'date': str(dt.date()), 'amount': round(float(amt), 4), 'yield': round(float(amt) / ref * 100, 2),
                       'pre_close': ref, 'fill_days': fill, 'filled': not hit.empty})
    months = sorted({int(x.month) for x in d[d.index > now - pd.Timedelta(days=730)].index})
    ttm = float(last12.sum()) if len(last12) else 0.0
    fwd = float(d.iloc[-1]) * round(per_year) if len(d) and per_year else None
    # 近一年含息報酬：價格變化 + 期間配息
    ret1y = None
    p1 = px[px.index <= now - pd.Timedelta(days=365)]
    if len(p1) and price:
        ret1y = round(((price + ttm) / float(p1.iloc[-1]) - 1) * 100, 1)
    fills = [e['fill_days'] for e in events if e.get('fill_days') is not None]
    return {'freq': freq, 'months': months, 'ttm': round(ttm, 4),
            'yield_ttm': round(ttm / price * 100, 2) if price and ttm else None,
            'yield_fwd': round(fwd / price * 100, 2) if price and fwd else None,
            'ret_1y': ret1y, 'events': events[::-1],
            'avg_fill_days': round(float(np.mean(fills)), 1) if fills else None,
            'unfilled': sum(1 for e in events if e.get('filled') is False)}


# ── 除息預告 ──────────────────────────────────────────────────────────
def upcoming():
    def fetch():
        out = []
        try:
            js = requests.get('https://www.twse.com.tw/rwd/zh/exRight/TWT48U?response=json', headers=HDRS, timeout=15).json()
            for r in js.get('data', []):
                if r[1].startswith('00') and '息' in r[3]:
                    out.append({'date': _roc(r[0]), 'code': r[1], 'name': r[2], 'amount': _num(re.sub(r'<[^>]+>', '', r[7])), 'market': '上市'})
        except Exception as e:
            print(f'[etf] TWT48U: {e}')
        try:
            js = requests.get('https://www.tpex.org.tw/web/stock/exright/preAnnounce/PrePost_result.php?l=zh-tw&o=json',
                              headers=HDRS, timeout=15).json()
            for r in (js.get('tables') or [{}])[0].get('data', []):
                if r[1].startswith('00') and '息' in r[3]:
                    out.append({'date': _roc(r[0]), 'code': r[1], 'name': r[2], 'amount': _num(r[7]), 'market': '上櫃'})
        except Exception as e:
            print(f'[etf] TPEx: {e}')
        today = pd.Timestamp.now(tz='Asia/Taipei').strftime('%Y-%m-%d')
        return sorted([x for x in out if x['date'] and x['date'] >= today], key=lambda x: (x['date'], x['code']))
    return _cached('upcoming', 3600, fetch)


# ── 比較 ──────────────────────────────────────────────────────────────
def compare(codes, weights=None):
    q = all_etf()
    up = {u['code']: u for u in upcoming()}
    items, H = [], {}
    for c in codes:
        base = q.get(c)
        if not base:
            continue
        h = holdings(c) or {'rows': [], 'date': None}
        stocks = [r for r in h['rows'] if r['code']]
        H[c] = {r['code']: r['pct'] for r in stocks}
        price = base['price'] or base['prev_nav']
        dv = dividend_stats(c, price)
        top10 = sum(r['pct'] for r in sorted(stocks, key=lambda r: -r['pct'])[:10])
        inds = _industries([r['code'] for r in stocks])
        sec = {}
        for r in stocks:
            sec[inds.get(r['code']) or '其他'] = sec.get(inds.get(r['code']) or '其他', 0) + r['pct']
        items.append(dict(base, holdings_date=h['date'], n_holdings=len(h['rows']), n_stocks=len(stocks), top10=round(top10, 1),
                          holdings=h['rows'][:60], sectors=sorted(({'name': k, 'pct': round(v, 1)} for k, v in sec.items()), key=lambda x: -x['pct'])[:8],
                          div=dv, next_div=up.get(c), premium_warn=bool(base['premium'] is not None and abs(base['premium']) >= 1)))
    codes = [i['code'] for i in items]
    # 重疊度：Σ min(兩檔對同一成分股的權重)，0～100%
    overlap = [[round(sum(min(H[a].get(k, 0), H[b].get(k, 0)) for k in set(H[a]) & set(H[b])), 1) if a != b else 100.0
                for b in codes] for a in codes]
    common = []
    if len(codes) >= 2:
        keys = set.intersection(*(set(H[c]) for c in codes)) if all(H[c] for c in codes) else set()
        names = {r['code']: r['name'] for i in items for r in i['holdings']}
        common = sorted(({'code': k, 'name': names.get(k, k), 'pcts': [H[c][k] for c in codes]} for k in keys),
                        key=lambda x: -sum(x['pcts']))[:20]
    # 合併曝險：依投資比例（預設平均）把各 ETF 的成分股加總
    w = weights if weights and len(weights) == len(codes) else [1 / len(codes)] * len(codes) if codes else []
    tot = sum(w) or 1
    exp = {}
    names = {}
    for c, wi in zip(codes, w):
        for i in items:
            if i['code'] == c:
                for r in i['holdings']:
                    if r['code']:
                        exp[r['code']] = exp.get(r['code'], 0) + r['pct'] * wi / tot
                        names[r['code']] = r['name']
    exposure = sorted(({'code': k, 'name': names[k], 'pct': round(v, 2)} for k, v in exp.items()), key=lambda x: -x['pct'])[:15]
    return {'items': items, 'codes': codes, 'overlap': overlap, 'common': common, 'exposure': exposure,
            'weights': [round(x / tot * 100, 1) for x in w]}


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/etf/list')
def api_list():
    try:
        rows = list(all_etf().values())
    except Exception as e:
        return jsonify(error=f'證交所 ETF 資料讀取失敗：{e}'), 503
    return jsonify(rows=rows, popular=POPULAR)


@bp.route('/api/etf/compare')
def api_compare():
    codes = [c.strip().upper() for c in request.args.get('codes', '').split(',') if c.strip()][:5]
    codes = [c for c in codes if re.fullmatch(r'00\d{2,4}[A-Z]?', c)]
    if not codes:
        return jsonify(error='請輸入 ETF 代號，例如 0050,00878'), 400
    w = [_num(x) or 0 for x in request.args.get('w', '').split(',')] if request.args.get('w') else None
    try:
        res = compare(codes, w)
    except Exception as e:
        return jsonify(error=f'分析失敗：{e}'), 503
    if not res['items']:
        return jsonify(error='查不到這些 ETF'), 404
    return jsonify(res)


@bp.route('/api/etf/calendar')
def api_calendar():
    return jsonify(upcoming=upcoming())


def init(app, login_required):
    @app.route('/etf')
    @login_required
    def etf_page():
        return render_template('etf.html')

    app.register_blueprint(bp)
