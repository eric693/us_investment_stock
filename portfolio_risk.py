"""投資組合風險分析：集中度、Beta、最大回撤、相關性、各持股的財報建議與建議部位。

做法和基金經理人的風控報表一樣：
- 權重用目前市值（美股換成台幣）。
- 用近一年的日報酬，以「目前權重」回推組合的波動度、Beta、最大回撤、單日 95% VaR。
- 風險貢獻：每檔對組合波動度的貢獻比例（權重 × 邊際風險），比權重更能看出誰在主導風險。
- 相關性：兩兩相關係數，高於 0.7 視為「看似分散、其實同漲同跌」。
- 建議部位：風險平價（每檔分到相同的波動），以及「每檔最多虧帳戶 1%」的停損張數上限。
- 每檔附上最新一次財報分析的建議，和前一次比對看是否被降評。
"""
import time
import threading

import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, jsonify, request

import fa_scan
import financials
import picks
import tw_board

bp = Blueprint('portfolio_risk', __name__)

_px_cache, _lock = {}, threading.Lock()
RISK_PER_TRADE = 0.01          # 每檔停損最多虧掉帳戶的 1%
STOP_ATR = 2.0                 # 停損距離 = 2 倍 ATR
LIMITS = {'single': 0.20, 'sector': 0.35, 'corr': 0.7}
SECTOR_ZH = {'Information Technology': '資訊科技', 'Technology': '資訊科技', 'Health Care': '醫療保健', 'Healthcare': '醫療保健',
             'Financials': '金融', 'Financial Services': '金融', 'Consumer Discretionary': '非必需消費', 'Consumer Cyclical': '非必需消費',
             'Consumer Staples': '必需消費', 'Consumer Defensive': '必需消費', 'Communication Services': '通訊服務',
             'Industrials': '工業', 'Energy': '能源', 'Materials': '原物料', 'Basic Materials': '原物料',
             'Real Estate': '不動產', 'Utilities': '公用事業'}


def _yf_close(sym, period='1y'):
    with _lock:
        hit = _px_cache.get((sym, period))
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
    h = yf.Ticker(sym).history(period=period, interval='1d', auto_adjust=True)
    s = h['Close'].dropna() if not h.empty else pd.Series(dtype=float)
    if len(s):
        s.index = pd.to_datetime(s.index.tz_localize(None) if s.index.tz is not None else s.index).normalize()
    with _lock:
        _px_cache[(sym, period)] = (time.time(), s)
    return s


def _bars(ticker, market):
    """近一年日 K（收盤、最高、最低）。台股、S&P 500 用本地資料庫，其他向 Yahoo 抓。"""
    since = (pd.Timestamp.now() - pd.Timedelta(days=400)).strftime('%Y-%m-%d')
    code = ticker.split('.')[0] if market == 'tw' else ticker
    if market == 'tw':
        with tw_board._db() as con:
            df = pd.read_sql_query('SELECT date, high, low, close FROM daily WHERE code=? AND date>=? ORDER BY date', con, params=(code, since))
    else:
        with picks._us_db() as con:
            df = pd.read_sql_query('SELECT date, high, low, close FROM us_daily WHERE code=? AND date>=? ORDER BY date', con, params=(code, since))
    if len(df) < 60:
        h = yf.Ticker(ticker).history(period='1y', interval='1d', auto_adjust=True)
        if h.empty:
            return None
        h.index = pd.to_datetime(h.index.tz_localize(None) if h.index.tz is not None else h.index).normalize()
        df = pd.DataFrame({'date': h.index, 'high': h['High'].values, 'low': h['Low'].values, 'close': h['Close'].values})
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').astype(float)


def _sector(code, market, ticker):
    if market == 'tw':
        with tw_board._db() as con:
            r = con.execute('SELECT name, industry FROM stocks WHERE code=?', (code,)).fetchone()
        return (r[0], r[1] or '其他') if r else (code, '其他')
    with picks._us_db() as con:
        r = con.execute('SELECT name, sector FROM us_stocks WHERE code=?', (code,)).fetchone()
    if r and r[1]:
        return r[0], '美股・' + SECTOR_ZH.get(r[1], r[1])
    try:
        i = financials.CachedTicker(ticker)
        info = i.info or {}
        i.save()
        sec = info.get('sector')
        return info.get('shortName') or code, ('美股・' + SECTOR_ZH.get(sec, sec)) if sec else '其他'
    except Exception:
        return code, '其他'


def _ratings(codes):
    """每檔最新一次與前一次的財報建議（來自每晚掃描；沒掃到的不在這裡）。"""
    out = {}
    with fa_scan._db() as con:
        for c in codes:
            rows = con.execute('SELECT date, rec, score, fair, buy_below FROM snaps WHERE code=? ORDER BY date DESC LIMIT 2', (c,)).fetchall()
            if rows:
                out[c] = {'date': rows[0][0], 'rec': rows[0][1], 'score': rows[0][2], 'fair': rows[0][3],
                          'buy_below': rows[0][4], 'prev': rows[1][1] if len(rows) > 1 else None}
    return out


def _max_dd(v):
    peak = np.maximum.accumulate(v)
    dd = v / peak - 1
    i = int(np.argmin(dd))
    j = int(np.argmax(v[:i + 1])) if i > 0 else 0
    return float(dd[i]), j, i


def analyze(holdings):
    rows, missing = [], []
    for h in holdings:
        ticker, market, _, _ = financials._resolve(str(h.get('ticker', '')))
        shares = float(h.get('shares') or 0)
        if not ticker or shares <= 0:
            continue
        code = ticker.split('.')[0] if market == 'tw' else ticker
        bars = _bars(ticker, market)
        if bars is None or len(bars) < 60:
            missing.append(h.get('ticker'))
            continue
        name, sector = _sector(code, market, ticker)
        rows.append({'ticker': ticker, 'code': code, 'market': market, 'name': name, 'sector': sector, 'shares': shares,
                     'cost': float(h.get('costPrice') or 0), 'bars': bars})
    # 同一檔分好幾筆買進的，合併成一筆
    merged = {}
    for r in rows:
        m = merged.get(r['ticker'])
        if m:
            tot = m['shares'] + r['shares']
            m['cost'] = (m['cost'] * m['shares'] + r['cost'] * r['shares']) / tot if tot else 0
            m['shares'] = tot
        else:
            merged[r['ticker']] = r
    rows = list(merged.values())
    if not rows:
        return None, '沒有可分析的持股（請確認代碼與股數；台股可直接輸入 2330）'

    fx = _yf_close('TWD=X')
    usdtwd = float(fx.iloc[-1]) if len(fx) else 32.0
    closes = {}
    for r in rows:
        c = r['bars']['close']
        if r['market'] == 'us':
            c = c * fx.reindex(c.index, method='ffill').fillna(usdtwd) if len(fx) else c * usdtwd
        closes[r['ticker']] = c
        r['price'] = float(r['bars']['close'].iloc[-1])
        r['value'] = r['price'] * r['shares'] * (usdtwd if r['market'] == 'us' else 1)
        hl = r['bars']
        tr = np.maximum(hl['high'] - hl['low'], np.maximum(abs(hl['high'] - hl['close'].shift()), abs(hl['low'] - hl['close'].shift())))
        r['atr'] = float(tr.rolling(14).mean().iloc[-1])
        r['pnl_pct'] = (r['price'] / r['cost'] - 1) * 100 if r['cost'] else None
        r['dd_from_high'] = (r['price'] / float(hl['close'].max()) - 1) * 100

    total = sum(r['value'] for r in rows)
    w = np.array([r['value'] / total for r in rows])
    px = pd.DataFrame(closes).sort_index().ffill().dropna(how='any').iloc[-252:]
    rets = px.pct_change().dropna()
    tickers = [r['ticker'] for r in rows]
    rets = rets[tickers]

    # 基準：台股加權、S&P 500（換台幣前的原始指數報酬即可）
    bench = {}
    for key, sym in (('taiex', '^TWII'), ('spx', '^GSPC')):
        b = _yf_close(sym)
        if len(b):
            bench[key] = b.pct_change().reindex(rets.index).fillna(0)
    port = rets.values @ w
    port_s = pd.Series(port, index=rets.index)
    cov = np.cov(rets.values.T) * 252 if len(rows) > 1 else np.array([[rets.values.var() * 252]])
    vol_p = float(np.sqrt(w @ cov @ w))
    mrc = cov @ w / vol_p if vol_p else np.zeros(len(w))
    contrib = w * mrc / vol_p if vol_p else w
    vols = np.sqrt(np.diag(cov))
    nav = np.cumprod(1 + port)
    mdd, dd_start, dd_end = _max_dd(nav)
    var95 = float(-np.percentile(port, 5))

    def beta(x, b):
        return float(np.cov(x, b)[0, 1] / np.var(b)) if np.var(b) > 0 else None
    betas = {k: beta(port, v.values) for k, v in bench.items()}
    tw_w = float(sum(wi for wi, r in zip(w, rows) if r['market'] == 'tw'))
    main_bench = 'taiex' if tw_w >= 0.5 else 'spx'

    corr = rets.corr().values if len(rows) > 1 else np.array([[1.0]])
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if corr[i, j] >= LIMITS['corr']:
                pairs.append({'a': rows[i]['code'], 'b': rows[j]['code'], 'corr': round(float(corr[i, j]), 2)})
    pairs.sort(key=lambda p: -p['corr'])
    avg_corr = float(corr[np.triu_indices(len(rows), 1)].mean()) if len(rows) > 1 else None
    div_ratio = float((w @ vols) / vol_p) if vol_p else None          # 分散比：> 1 越多代表分散效果越好

    sectors = {}
    for wi, r in zip(w, rows):
        sectors[r['sector']] = sectors.get(r['sector'], 0) + wi
    sectors = sorted(({'name': k, 'weight': round(v * 100, 1)} for k, v in sectors.items()), key=lambda x: -x['weight'])
    hhi = float((w ** 2).sum())

    # 建議部位：風險平價權重（反比於波動度），與每檔停損最多虧帳戶 1% 的股數上限
    inv = 1 / np.where(vols > 0, vols, np.nan)
    rp = np.nan_to_num(inv / np.nansum(inv))
    ratings = _ratings([r['code'] for r in rows])
    out_rows = []
    for i, r in enumerate(rows):
        fx_r = usdtwd if r['market'] == 'us' else 1
        max_sh = total * RISK_PER_TRADE / (STOP_ATR * r['atr'] * fx_r) if r['atr'] else None
        target_sh = rp[i] * total / (r['price'] * fx_r)
        rt = ratings.get(r['code'])
        out_rows.append({
            'ticker': r['ticker'], 'code': r['code'], 'name': r['name'], 'market': r['market'], 'sector': r['sector'],
            'shares': r['shares'], 'price': round(r['price'], 2), 'value': round(r['value']), 'weight': round(w[i] * 100, 1),
            # 個股 Beta 對自己市場的大盤：台股對加權、美股對 S&P 500
            'vol': round(float(vols[i]) * 100, 1),
            'beta': (lambda b: round(beta(rets.iloc[:, i].values, bench[b].values), 2) if b in bench else None)('taiex' if r['market'] == 'tw' else 'spx'),
            'risk_contrib': round(float(contrib[i]) * 100, 1), 'rp_weight': round(float(rp[i]) * 100, 1),
            'target_shares': round(target_sh) if target_sh else None,
            'max_shares': None if max_sh is None else round(max_sh),
            'stop': round(r['price'] - STOP_ATR * r['atr'], 2) if r['atr'] else None,
            'pnl_pct': None if r['pnl_pct'] is None else round(r['pnl_pct'], 1), 'dd_from_high': round(r['dd_from_high'], 1),
            'rating': rt,
        })

    warn = []
    for o in out_rows:
        if o['weight'] > LIMITS['single'] * 100:
            warn.append(f"{o['name']} 佔 {o['weight']}%，單一持股超過 {int(LIMITS['single'] * 100)}%")
        if o['risk_contrib'] > max(30, o['weight'] * 1.5):
            warn.append(f"{o['name']} 權重 {o['weight']}% 卻貢獻 {o['risk_contrib']}% 的風險，波動特別大")
        if o['rating'] and o['rating']['rec'] == 'avoid':
            warn.append(f"{o['name']} 最新財報分析為「避開」")
        if o['rating'] and o['rating']['prev'] and _rank(o['rating']['rec']) < _rank(o['rating']['prev']):
            warn.append(f"{o['name']} 財報建議被降評（{_lbl(o['rating']['prev'])} → {_lbl(o['rating']['rec'])}）")
        if o['max_shares'] is not None and o['shares'] > o['max_shares'] * 1.5:
            warn.append(f"{o['name']} 持有 {o['shares']:,.0f} 股，超過風險上限約 {o['max_shares']:,.0f} 股（以 2 倍 ATR 停損、每檔最多虧 1% 計）")
    for s in sectors:
        if s['weight'] > LIMITS['sector'] * 100 and len(sectors) > 1:
            warn.append(f"{s['name']} 產業佔 {s['weight']}%，超過 {int(LIMITS['sector'] * 100)}%")
        elif s['weight'] > LIMITS['sector'] * 100:
            warn.append(f"全部持股都在 {s['name']}，沒有產業分散")
    if pairs:
        warn.append(f"{len(pairs)} 組持股相關係數高於 {LIMITS['corr']}，看似分散其實同漲同跌（最高：{pairs[0]['a']} 與 {pairs[0]['b']} {pairs[0]['corr']}）")
    if betas.get(main_bench) and betas[main_bench] > 1.3:
        warn.append(f"組合 Beta {betas[main_bench]:.2f}，大盤跌 10% 時預期跌約 {betas[main_bench] * 10:.0f}%")

    nav_s = (nav / nav[0] - 1) * 100
    return {
        'total': round(total), 'usdtwd': round(usdtwd, 2), 'n': len(rows), 'missing': missing,
        'start': str(rets.index[0].date()), 'end': str(rets.index[-1].date()), 'days': len(rets),
        'vol': round(vol_p * 100, 1), 'ret_1y': round(float(nav[-1] - 1) * 100, 1),
        'sharpe': round(float(port.mean() * 252 / (vol_p or 1)), 2) if vol_p else None,
        'beta': {k: None if v is None else round(v, 2) for k, v in betas.items()}, 'main_bench': main_bench,
        'max_dd': round(mdd * 100, 1), 'dd_from': str(rets.index[dd_start].date()), 'dd_to': str(rets.index[dd_end].date()),
        'var95': round(var95 * 100, 2), 'var95_amt': round(var95 * total),
        'eff_n': round(1 / hhi, 1), 'avg_corr': None if avg_corr is None else round(avg_corr, 2),
        'div_ratio': None if div_ratio is None else round(div_ratio, 2),
        'tw_weight': round(tw_w * 100, 1), 'sectors': sectors, 'rows': out_rows, 'pairs': pairs[:10], 'warnings': warn,
        'corr': {'labels': [r['code'] for r in rows], 'matrix': [[round(float(x), 2) for x in row] for row in corr]},
        'nav': [[str(d.date()), round(float(v), 2)] for d, v in zip(rets.index, nav_s)],
    }, None


def _rank(rec):
    return {'avoid': 0, 'hold': 1, 'accumulate': 2, 'buy': 3}.get(rec, 1)


def _lbl(rec):
    return {'buy': '買進', 'accumulate': '逢低布局', 'hold': '持有觀望', 'avoid': '避開'}.get(rec, rec)


@bp.route('/api/portfolio/risk', methods=['POST'])
def api_risk():
    hs = (request.get_json(silent=True) or {}).get('holdings') or []
    if not isinstance(hs, list) or len(hs) > 60:
        return jsonify(error='持股清單格式不正確（最多 60 檔）'), 400
    try:
        res, err = analyze(hs)
    except Exception as e:
        return jsonify(error=f'分析失敗：{e}'), 503
    return (jsonify(error=err), 400) if err else jsonify(res)


def init(app, login_required):
    app.register_blueprint(bp)
