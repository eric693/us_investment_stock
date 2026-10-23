"""財報分析：用基本面經理人的流程替台股、美股打分並給出操作建議。

流程（和買方研究員寫個股報告的順序一樣）：
1. 成長性：營收、EPS 的年增與三年複合成長；台股另看月營收；分析師對未來 12 個月的預估。
2. 獲利能力：毛利率、營益率、淨利率的水準與趨勢；ROE、ROIC。
3. 財務體質：淨負債 / EBITDA、流動比、利息保障倍數、Altman Z。
4. 盈餘品質：營業現金流 / 淨利、自由現金流率、Piotroski F-score；美股另扣股票酬勞。
5. 估值：本益比（現在、預估）、與同產業中位數及自己過去的比較、PEG、EV/EBITDA、自由現金流殖利率、殖利率。

合理價取三種方法：本益比法（預估 EPS × 合理本益比）、現金流折現（DCF）、分析師目標價，
再依「公司品質」與「價格相對合理價的空間」決定建議；有財務警訊時直接降級。
金融股（銀行、保險）改用 ROE 與股價淨值比估值，不看毛利率、負債比與自由現金流。

這是規則化的分析，參數是業界常用的經驗值，不是用歷史股價最佳化出來的；財報資料來自 Yahoo Finance，
台股同業估值來自證交所／櫃買中心。
"""
import os
import re
import time
import json
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, jsonify, request, render_template

import tw_board

bp = Blueprint('financials', __name__)

TTL = 6 * 3600
_cache, _lock = {}, threading.Lock()
_sector_cache = {}
DISCOUNT = {'tw': 0.085, 'us': 0.09}          # 折現率：無風險利率 + 股權風險溢酬的常用值
TERMINAL = {'tw': 0.02, 'us': 0.025}          # 永續成長率
SAFETY = 0.15                                 # 安全邊際：合理價打 85 折以下才叫便宜


# ── Yahoo 資料的硬碟快取 ──────────────────────────────────────────────
# 財報一季才變一次，每晚掃描上千檔時不必每次重抓：財報與月線 7 天、報價與分析師預估 20 小時。
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fa_cache')
RAW_TTL = {'info': 72000, 'earnings_estimate': 72000, 'eps_trend': 72000, 'earnings_history': 72000,
           'recommendations_summary': 72000}
STATEMENT_TTL = 7 * 86400


class CachedTicker:
    """和 yf.Ticker 一樣的用法，但每個欄位先看硬碟快取。"""

    def __init__(self, ticker, fresh=False):
        self.ticker, self.fresh = ticker, fresh
        self._t = None
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, re.sub(r'[^A-Za-z0-9.\-]', '_', ticker) + '.pkl')
        try:
            with open(self.path, 'rb') as f:
                self._d = pickle.load(f)
        except Exception:
            self._d = {}
        self._dirty = False

    @property
    def yt(self):
        if self._t is None:
            self._t = yf.Ticker(self.ticker)
        return self._t

    def _get(self, name, fetch):
        ttl = RAW_TTL.get(name, STATEMENT_TTL)
        hit = self._d.get(name)
        if hit and not self.fresh and time.time() - hit[0] < ttl:
            return hit[1]
        val = fetch()
        self._d[name] = (time.time(), val)
        self._dirty = True
        return val

    def __getattr__(self, name):
        if name.startswith('_') or name in ('ticker', 'fresh', 'path'):
            raise AttributeError(name)
        return self._get(name, lambda: getattr(self.yt, name))

    def history(self, **kw):
        return self._get('history:' + json.dumps(kw, sort_keys=True), lambda: self.yt.history(**kw))

    def save(self):
        if not self._dirty:
            return
        tmp = self.path + f'.{os.getpid()}.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump(self._d, f)
        os.replace(tmp, self.path)


# ── 讀財報 ────────────────────────────────────────────────────────────
def _row(df, *names):
    """財報欄位名稱在不同公司會不一樣，依序找第一個有的；回傳由舊到新排序的 Series。"""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors='coerce')
            return s.sort_index()
    return pd.Series(dtype=float)


def _last(s, k=1):
    s = s.dropna()
    return float(s.iloc[-k]) if len(s) >= k else None


def _ttm(q):
    q = q.dropna()
    return float(q.iloc[-4:].sum()) if len(q) >= 4 else None


def _div(a, b):
    if a is None or b in (None, 0) or (isinstance(b, float) and np.isnan(b)):
        return None
    v = a / b
    return None if np.isnan(v) or np.isinf(v) else float(v)


def _pct(v, d=1):
    return None if v is None else round(v * 100, d)


def _resolve(code):
    """'2330'、'6488'、'2330.TW'、'AAPL' → (Yahoo 代碼, 市場, 名稱, 產業)。"""
    raw = code.strip().upper()
    base = re.sub(r'\.(TW|TWO)$', '', raw)
    if re.fullmatch(r'\d{4,6}[A-Z]?', base):
        with tw_board._db() as con:
            r = con.execute('SELECT name, market, industry FROM stocks WHERE code=?', (base,)).fetchone()
        if r:
            return f"{base}.{'TWO' if r[1] == 'OTC' else 'TW'}", 'tw', r[0], r[2]
        return (raw if '.' in raw else f'{base}.TW'), 'tw', None, None
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', raw):
        return None, None, None, None
    return raw, 'us', None, None


# ── 同業估值 ──────────────────────────────────────────────────────────
def _tw_industry(industry, code):
    """台股同產業的本益比、股價淨值比、殖利率中位數（證交所／櫃買中心每日公布）。"""
    if not industry:
        return None
    with tw_board._db() as con:
        rows = con.execute('''SELECT v.pe, v.pb, v.dy FROM valuation v JOIN stocks s ON s.code = v.code
                              WHERE s.industry = ? AND s.kind = 'stock' AND v.code != ?''', (industry, code)).fetchall()
    if len(rows) < 5:
        return None
    pe = [r[0] for r in rows if r[0] and 0 < r[0] < 200]
    pb = [r[1] for r in rows if r[1] and r[1] > 0]
    dy = [r[2] for r in rows if r[2] is not None]
    return {'name': industry, 'n': len(rows), 'pe': _med(pe), 'pb': _med(pb), 'dy': _med(dy)}


def _med(xs):
    return round(float(np.median(xs)), 2) if len(xs) >= 3 else None


# Yahoo 的類股名稱 → 標普 GICS 名稱（Nasdaq-100 清單用的是 Yahoo 那套，兩種都要比）
GICS = {'Technology': 'Information Technology', 'Healthcare': 'Health Care', 'Financial Services': 'Financials',
        'Consumer Cyclical': 'Consumer Discretionary', 'Consumer Defensive': 'Consumer Staples',
        'Basic Materials': 'Materials'}


def _us_sector(sector, code):
    """美股同類股（S&P 500 + Nasdaq-100 同 GICS 類股）估值中位數：抽市值前 30 檔，快取一天。"""
    if not sector:
        return None
    hit = _sector_cache.get(sector)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    import picks
    with picks._us_db() as con:
        codes = [r[0] for r in con.execute('SELECT code FROM us_stocks WHERE sector IN (?, ?) AND code != ?',
                                           (sector, GICS.get(sector, sector), code)).fetchall()]
    if len(codes) < 5:
        return None

    def info(c):
        try:
            ct = CachedTicker(c)
            i = ct.info
            ct.save()
            return (i.get('marketCap') or 0, i.get('trailingPE'), i.get('forwardPE'), i.get('priceToBook'),
                    i.get('enterpriseToEbitda'), i.get('priceToSalesTrailing12Months'))
        except Exception:
            return None
    with ThreadPoolExecutor(8) as ex:
        got = [x for x in ex.map(info, codes[:80]) if x]
    got = sorted(got, key=lambda x: -x[0])[:30]
    col = lambda k, hi: _med([g[k] for g in got if g[k] and 0 < g[k] < hi])
    res = {'name': sector, 'n': len(got), 'pe': col(1, 150), 'fpe': col(2, 150), 'pb': col(3, 100),
           'ev_ebitda': col(4, 100), 'ps': col(5, 60)}
    _sector_cache[sector] = (time.time(), res)
    return res


# ── 計分工具 ──────────────────────────────────────────────────────────
def _band(v, pts):
    """依門檻線性內插成 0～100 分。pts = [(數值, 分數), ...] 由小到大。"""
    if v is None:
        return None
    xs, ys = zip(*pts)
    return float(np.interp(v, xs, ys))


def _avg(scores):
    xs = [s for s in scores if s is not None]
    return round(sum(xs) / len(xs)) if xs else None


def _m(key, label, value, score, fmt, note, good=None):
    return {'key': key, 'label': label, 'value': value, 'score': None if score is None else round(score),
            'fmt': fmt, 'note': note, 'good': good}


# ── 主分析 ────────────────────────────────────────────────────────────
def analyze(code, price=None, use_cache=True):
    """price：批次掃描時用本地資料庫的收盤價（不另外查報價）。"""
    ticker, market, tw_name, tw_ind = _resolve(code)
    if not ticker:
        return None, '代碼格式不正確'
    with _lock:
        hit = _cache.get(ticker)
        if use_cache and price is None and hit and time.time() - hit[0] < TTL:
            return hit[1], None
    t = CachedTicker(ticker)
    try:
        res, err = _analyze(t, ticker, market, tw_name, tw_ind, price)
    finally:
        t.save()
    if res and price is None:
        with _lock:
            _cache[ticker] = (time.time(), res)
    return res, err


def _analyze(t, ticker, market, tw_name, tw_ind, price):
    info = t.info or {}
    price = price or info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
    if not price:
        return None, '查不到這檔股票的報價'
    A_is, Q_is = t.income_stmt, t.quarterly_income_stmt
    A_bs, Q_bs = t.balance_sheet, t.quarterly_balance_sheet
    A_cf, Q_cf = t.cashflow, t.quarterly_cashflow
    if A_is is None or A_is.empty:
        return None, '沒有財報資料（ETF、權證或剛上市的公司沒有完整財報）'

    sector = info.get('sector') or ''
    financial = sector == 'Financial Services' or '金融' in (tw_ind or '')
    cur = info.get('financialCurrency') or info.get('currency') or ''

    # 年度與季度序列
    rev_a, rev_q = _row(A_is, 'Total Revenue', 'Operating Revenue'), _row(Q_is, 'Total Revenue', 'Operating Revenue')
    gp_a, gp_q = _row(A_is, 'Gross Profit'), _row(Q_is, 'Gross Profit')
    op_a, op_q = _row(A_is, 'Operating Income', 'Total Operating Income As Reported'), _row(Q_is, 'Operating Income', 'Total Operating Income As Reported')
    ni_a, ni_q = _row(A_is, 'Net Income Common Stockholders', 'Net Income'), _row(Q_is, 'Net Income Common Stockholders', 'Net Income')
    eps_a, eps_q = _row(A_is, 'Diluted EPS', 'Basic EPS'), _row(Q_is, 'Diluted EPS', 'Basic EPS')
    ebit_a = _row(A_is, 'EBIT', 'Operating Income')
    ebitda_a, ebitda_q = _row(A_is, 'EBITDA', 'Normalized EBITDA'), _row(Q_is, 'EBITDA', 'Normalized EBITDA')
    int_a = _row(A_is, 'Interest Expense', 'Interest Expense Non Operating')
    tax_rate = _last(_row(A_is, 'Tax Rate For Calcs')) or 0.2
    ocf_a, ocf_q = _row(A_cf, 'Operating Cash Flow', 'Cash Flow From Continuing Operating Activities'), _row(Q_cf, 'Operating Cash Flow', 'Cash Flow From Continuing Operating Activities')
    fcf_a, fcf_q = _row(A_cf, 'Free Cash Flow'), _row(Q_cf, 'Free Cash Flow')
    sbc_a = _row(A_cf, 'Stock Based Compensation')
    div_paid_a = _row(A_cf, 'Cash Dividends Paid', 'Common Stock Dividend Paid')
    buyback_a = _row(A_cf, 'Repurchase Of Capital Stock', 'Common Stock Payments')
    eq_a, eq_q = _row(A_bs, 'Stockholders Equity', 'Common Stock Equity'), _row(Q_bs, 'Stockholders Equity', 'Common Stock Equity')
    ta_a, ta_q = _row(A_bs, 'Total Assets'), _row(Q_bs, 'Total Assets')
    tl_q = _row(Q_bs, 'Total Liabilities Net Minority Interest')
    debt_q = _row(Q_bs, 'Total Debt')
    cash_q = _row(Q_bs, 'Cash Cash Equivalents And Short Term Investments', 'Cash And Cash Equivalents')
    ca_a, cl_a = _row(A_bs, 'Current Assets'), _row(A_bs, 'Current Liabilities')
    ca_q, cl_q = _row(Q_bs, 'Current Assets'), _row(Q_bs, 'Current Liabilities')
    wc_q, re_q = _row(Q_bs, 'Working Capital'), _row(Q_bs, 'Retained Earnings')
    ic_q = _row(Q_bs, 'Invested Capital')
    ltd_a = _row(A_bs, 'Long Term Debt', 'Long Term Debt And Capital Lease Obligation')
    shares_a = _row(A_bs, 'Ordinary Shares Number', 'Share Issued')

    shares = info.get('sharesOutstanding') or _last(shares_a)
    mcap = info.get('marketCap') or (price * shares if shares else None)
    rev_ttm, ni_ttm = _ttm(rev_q) or _last(rev_a), _ttm(ni_q) or _last(ni_a)
    op_ttm, gp_ttm = _ttm(op_q) or _last(op_a), _ttm(gp_q) or _last(gp_a)
    ocf_ttm, fcf_ttm = _ttm(ocf_q) or _last(ocf_a), _ttm(fcf_q) or _last(fcf_a)
    ebitda_ttm = _ttm(ebitda_q) or _last(ebitda_a)
    eps_ttm = _ttm(eps_q) or info.get('trailingEps') or _last(eps_a)
    equity = _last(eq_q) or _last(eq_a)
    equity_prev = _last(eq_q, 5) or _last(eq_a, 2)
    debt, cash = _last(debt_q) or 0.0, _last(cash_q) or 0.0
    net_debt = debt - cash

    # 分析師預估
    # 「未來 12 個月」EPS：依目前會計年度已過的比例，把今年度與下年度預估加權（研究報告常用的 NTM）
    fwd_eps, fwd_growth, n_est, eps_rev, this_year_growth = info.get('forwardEps'), None, 0, None, None
    try:
        ee = t.earnings_estimate
        if ee is not None and not ee.empty and '+1y' in ee.index and '0y' in ee.index:
            fye = info.get('lastFiscalYearEnd')
            frac = min(max((time.time() - fye) / (365.25 * 86400), 0), 1) if fye else 0.5
            e0, e1 = float(ee.loc['0y', 'avg']), float(ee.loc['+1y', 'avg'])
            g0, g1 = ee.loc['0y', 'growth'], ee.loc['+1y', 'growth']
            if e0 and e1 and not np.isnan(e0) and not np.isnan(e1):
                fwd_eps = (1 - frac) * e0 + frac * e1
            n_est = int(ee.loc['+1y', 'numberOfAnalysts'] or 0)
            gs = [(w, float(g)) for w, g in ((1 - frac, g0), (frac, g1)) if g is not None and not np.isnan(g)]
            fwd_growth = sum(w * g for w, g in gs) / sum(w for w, _ in gs) if gs else None
            this_year_growth = float(g0) if g0 is not None and not np.isnan(g0) else None
        et = t.eps_trend
        if et is not None and not et.empty and '+1y' in et.index and et.loc['+1y', '90daysAgo']:
            eps_rev = float(et.loc['+1y', 'current'] / et.loc['+1y', '90daysAgo'] - 1)
    except Exception:
        this_year_growth = None
    surprises = []
    try:
        eh = t.earnings_history
        if eh is not None and not eh.empty:
            surprises = [float(x) for x in eh['surprisePercent'].dropna().tolist()]
    except Exception:
        pass

    # ── 1. 成長性 ──
    g_metrics = []
    rev_yoy_q = _div(_last(rev_q), _last(rev_q, 5)) if len(rev_q.dropna()) >= 5 else None
    rev_yoy_q = None if rev_yoy_q is None else rev_yoy_q - 1
    ra = rev_a.dropna()
    rev_cagr = (ra.iloc[-1] / ra.iloc[-4]) ** (1 / 3) - 1 if len(ra) >= 4 and ra.iloc[-4] > 0 else (
        (ra.iloc[-1] / ra.iloc[0]) ** (1 / (len(ra) - 1)) - 1 if len(ra) >= 2 and ra.iloc[0] > 0 else None)
    eq_ = eps_q.dropna()
    eps_yoy_q = (eq_.iloc[-1] / eq_.iloc[-5] - 1) if len(eq_) >= 5 and eq_.iloc[-5] > 0 else None
    if financial:
        # 金融業的「營收」含投資損益，波動大又不代表本業，改看每股淨值成長
        ea = eq_a.dropna()
        bv_cagr = (ea.iloc[-1] / ea.iloc[0]) ** (1 / (len(ea) - 1)) - 1 if len(ea) >= 2 and ea.iloc[0] > 0 else None
        g_metrics.append(_m('bv_cagr', f'淨值 {max(len(ea) - 1, 1)} 年複合成長', _pct(bv_cagr), _band(bv_cagr, [(-0.05, 0), (0, 30), (0.06, 60), (0.1, 85), (0.15, 100)]), '%',
                            '金融股看淨值能不能穩定長大，營收含投資損益不準'))
        rev_yoy_q = rev_cagr = None
    else:
        g_metrics.append(_m('rev_yoy_q', '最新一季營收年增', _pct(rev_yoy_q), _band(rev_yoy_q, [(-0.15, 0), (0, 35), (0.1, 65), (0.25, 90), (0.4, 100)]), '%',
                            '和去年同一季比，排除季節性'))
        g_metrics.append(_m('rev_cagr', '營收 3 年複合成長', _pct(rev_cagr), _band(rev_cagr, [(-0.1, 0), (0, 30), (0.08, 60), (0.15, 85), (0.3, 100)]), '%',
                            '看長期趨勢，比單季穩定'))
    g_metrics.append(_m('eps_yoy_q', '最新一季 EPS 年增', _pct(eps_yoy_q), _band(eps_yoy_q, [(-0.3, 0), (0, 35), (0.15, 65), (0.3, 90), (0.6, 100)]), '%',
                        '獲利成長是否跟上營收'))
    g_metrics.append(_m('fwd_growth', '分析師預估未來 12 個月 EPS 成長', _pct(fwd_growth), _band(fwd_growth, [(-0.1, 10), (0, 35), (0.1, 65), (0.2, 85), (0.35, 100)]), '%',
                        f'{n_est} 位分析師共識' if n_est else '分析師共識'))
    if eps_rev is not None:
        g_metrics.append(_m('eps_rev', '近 90 天預估 EPS 調整', _pct(eps_rev), _band(eps_rev, [(-0.1, 0), (-0.02, 35), (0, 50), (0.03, 75), (0.08, 100)]), '%',
                            '分析師上修代表基本面比預期好，常領先股價'))
    tw_rev = None
    if market == 'tw' and not financial:
        tw_rev = _tw_monthly(ticker.split('.')[0])
        if tw_rev:
            g_metrics.append(_m('mrev3', '近 3 個月營收年增', tw_rev['yoy3'], _band(tw_rev['yoy3'] / 100 if tw_rev['yoy3'] is not None else None,
                                [(-0.15, 0), (0, 35), (0.1, 65), (0.25, 90), (0.4, 100)]), '%', f"月營收最新到 {tw_rev['last_ym']}，比季報早 1～2 個月反映"))

    # ── 2. 獲利能力 ──
    p_metrics = []
    gm, om, nm = _div(gp_ttm, rev_ttm), _div(op_ttm, rev_ttm), _div(ni_ttm, rev_ttm)
    roe = _div(ni_ttm, (equity + equity_prev) / 2 if equity and equity_prev else equity)
    ic = _last(ic_q)
    roic = _div((op_ttm or 0) * (1 - min(max(tax_rate, 0), 0.4)), ic) if ic and op_ttm else None
    om_a = (op_a / rev_a).dropna()
    om_trend = float(om_a.iloc[-1] - om_a.iloc[0]) if len(om_a) >= 3 else None
    gm_a = (gp_a / rev_a).dropna()
    if not financial:
        p_metrics.append(_m('gm', '毛利率（近四季）', _pct(gm), _band(gm, [(0.1, 15), (0.25, 45), (0.4, 70), (0.6, 95)]), '%',
                            '產品定價能力與競爭優勢'))
        p_metrics.append(_m('om', '營業利益率（近四季）', _pct(om), _band(om, [(0, 0), (0.05, 30), (0.15, 65), (0.3, 95)]), '%',
                            '本業賺錢的效率'))
        p_metrics.append(_m('om_trend', f'營益率 {len(om_a)} 年變化', None if om_trend is None else round(om_trend * 100, 1),
                            _band(om_trend, [(-0.08, 0), (-0.02, 35), (0, 55), (0.03, 80), (0.08, 100)]), '百分點',
                            '利潤率擴張代表規模經濟或產品組合變好'))
    p_metrics.append(_m('nm', '淨利率（近四季）', _pct(nm), _band(nm, [(0, 0), (0.05, 35), (0.12, 65), (0.25, 95)]), '%', '最後真正留給股東的比例'))
    p_metrics.append(_m('roe', 'ROE 股東權益報酬率', _pct(roe), _band(roe, [(0, 0), (0.08, 35), (0.15, 70), (0.25, 95)]), '%',
                        '巴菲特最看重的指標：長期 15% 以上屬優秀'))
    if roic is not None and not financial:
        p_metrics.append(_m('roic', 'ROIC 投入資本報酬率', _pct(roic), _band(roic, [(0, 0), (0.08, 40), (0.15, 75), (0.25, 100)]), '%',
                            '高於資金成本（約 8～9%）才是真正創造價值'))

    # ── 3. 財務體質 ──
    h_metrics, flags = [], []
    nd_ebitda = _div(net_debt, ebitda_ttm) if ebitda_ttm and ebitda_ttm > 0 else None
    cur_ratio = _div(_last(ca_q) or _last(ca_a), _last(cl_q) or _last(cl_a))
    ebit_ttm = op_ttm
    interest = abs(_last(int_a) or 0)
    coverage = _div(ebit_ttm, interest) if interest > 0 else None
    z = None
    ta, tl = _last(ta_q) or _last(ta_a), _last(tl_q)
    if not financial and ta and tl and mcap and rev_ttm:
        z = (1.2 * (_last(wc_q) or 0) / ta + 1.4 * (_last(re_q) or 0) / ta + 3.3 * (ebit_ttm or 0) / ta
             + 0.6 * mcap / tl + 1.0 * rev_ttm / ta)
    if not financial:
        h_metrics.append(_m('nd_ebitda', '淨負債 / EBITDA', None if nd_ebitda is None else round(nd_ebitda, 2),
                            _band(-(nd_ebitda if nd_ebitda is not None else 0), [(-4, 0), (-2.5, 40), (-1, 75), (0, 95), (1, 100)]) if nd_ebitda is not None else (100 if net_debt < 0 else None),
                            '倍', '負數代表現金多於負債；超過 3 倍要留意' if net_debt >= 0 else '現金多於負債（淨現金）', good=net_debt < 0))
        h_metrics.append(_m('cur_ratio', '流動比', None if cur_ratio is None else round(cur_ratio, 2),
                            _band(cur_ratio, [(0.6, 0), (1, 40), (1.5, 75), (2.5, 100)]), '倍', '一年內到期的資產 ÷ 負債，低於 1 倍短期資金較緊'))
        if coverage is not None:
            h_metrics.append(_m('coverage', '利息保障倍數', round(coverage, 1), _band(coverage, [(1, 0), (3, 40), (8, 75), (20, 100)]), '倍',
                                '營業利益能付幾次利息，低於 3 倍有壓力'))
        h_metrics.append(_m('altman_z', 'Altman Z 破產風險', None if z is None else round(z, 2),
                            _band(z, [(1.1, 0), (1.8, 30), (3, 75), (5, 100)]), '', '> 3 安全、1.8～3 灰色、< 1.8 危險（製造業公式）'))
        if nd_ebitda is not None and nd_ebitda > 3.5:
            flags.append(f'淨負債是 EBITDA 的 {nd_ebitda:.1f} 倍，槓桿偏高')
        if coverage is not None and coverage < 2:
            flags.append(f'利息保障倍數只有 {coverage:.1f} 倍，付息壓力大')
        if z is not None and z < 1.8:
            flags.append(f'Altman Z = {z:.2f}，落在財務危險區')
    else:
        lev = _div(ta, equity)
        h_metrics.append(_m('lev', '資產 / 淨值（槓桿）', None if lev is None else round(lev, 1), _band(-(lev or 0), [(-20, 20), (-14, 50), (-10, 75), (-6, 100)]) if lev else None,
                            '倍', '金融業本來就高槓桿，銀行約 10～15 倍屬正常'))

    # ── 4. 盈餘品質與現金流 ──
    q_metrics = []
    ocf_ni = _div(ocf_ttm, ni_ttm) if ni_ttm and ni_ttm > 0 else None
    fcf_margin = _div(fcf_ttm, rev_ttm)
    sbc = _last(sbc_a)
    fcf_adj = (fcf_ttm - sbc) if (market == 'us' and fcf_ttm is not None and sbc) else None
    fcf_pos_years = int((fcf_a.dropna() > 0).sum()) if len(fcf_a.dropna()) else None
    fscore, fparts = _piotroski(ni_a, ocf_a, ta_a, ltd_a, ca_a, cl_a, shares_a, gm_a, rev_a)
    if not financial:
        q_metrics.append(_m('ocf_ni', '營業現金流 / 淨利', None if ocf_ni is None else round(ocf_ni, 2),
                            _band(ocf_ni, [(0.3, 0), (0.7, 40), (1, 75), (1.3, 100)]), '倍', '大於 1 代表賺的是真金白銀，長期低於 0.8 要小心'))
        q_metrics.append(_m('fcf_margin', '自由現金流率', _pct(fcf_margin), _band(fcf_margin, [(-0.1, 0), (0, 30), (0.08, 65), (0.2, 100)]), '%',
                            '營業現金流扣掉資本支出後，真正能配息、買回、還債的錢'))
        if fcf_adj is not None and rev_ttm:
            q_metrics.append(_m('fcf_sbc', '扣股票酬勞後自由現金流率', _pct(fcf_adj / rev_ttm), _band(fcf_adj / rev_ttm, [(-0.1, 0), (0, 30), (0.08, 65), (0.2, 100)]), '%',
                                '美股科技股常用股票發薪，這部分其實是稀釋股東的成本'))
        if fcf_pos_years is not None:
            q_metrics.append(_m('fcf_years', '自由現金流為正的年數', f'{fcf_pos_years} / {len(fcf_a.dropna())}',
                                100 * fcf_pos_years / max(1, len(fcf_a.dropna())), '', '穩定的現金流讓估值更有把握'))
        if ocf_ni is not None and ocf_ni < 0.6 and ni_ttm and ni_ttm > 0:
            flags.append(f'營業現金流只有淨利的 {ocf_ni:.0%}，獲利沒有變成現金')
        if len(ocf_a.dropna()) >= 2 and (ocf_a.dropna().iloc[-2:] < 0).all():
            flags.append('連續兩年營業現金流為負')
    if fscore is not None:
        q_metrics.append(_m('fscore', 'Piotroski F-score', f'{fscore} / 9', _band(fscore, [(2, 0), (4, 35), (6, 70), (8, 100)]), '',
                            '九項財務體質改善檢查，8～9 分強、0～2 分弱'))
    if surprises:
        beat = sum(1 for s in surprises if s > 0)
        q_metrics.append(_m('surprise', f'近 {len(surprises)} 季 EPS 優於預期', f'{beat} / {len(surprises)}', 25 * beat * 4 / len(surprises) if surprises else None,
                            '', f'平均驚喜 {np.mean(surprises) * 100:+.1f}%'))

    # ── 5. 估值 ──
    peer = _tw_industry(tw_ind, ticker.split('.')[0]) if market == 'tw' else _us_sector(sector, ticker)
    pe = _div(price, eps_ttm) if eps_ttm and eps_ttm > 0 else None
    fpe = _div(price, fwd_eps) if fwd_eps and fwd_eps > 0 else None
    pb = _div(price * shares, equity) if shares and equity and equity > 0 else info.get('priceToBook')
    ev = (mcap or 0) + net_debt if mcap else None
    ev_ebitda = _div(ev, ebitda_ttm) if ev and ebitda_ttm and ebitda_ttm > 0 else None
    fcf_yield = _div(fcf_ttm, mcap)
    dy = info.get('dividendYield')                       # Yahoo 已是百分比
    dy = float(dy) if dy is not None else None
    g_for_peg = fwd_growth if fwd_growth is not None else this_year_growth
    if g_for_peg is None and rev_cagr is not None:
        g_for_peg = rev_cagr
    peg = _div(fpe or pe, g_for_peg * 100) if g_for_peg and g_for_peg > 0.01 and (fpe or pe) else None
    hist_pe = _hist_pe(t, eps_a)
    own_pe_med = _med([x['pe'] for x in hist_pe if x['pe'] and 0 < x['pe'] < 200]) if hist_pe else None
    v_metrics = []
    ref_pe = peer.get('pe') if peer else None
    v_metrics.append(_m('pe', '本益比（近四季）', None if pe is None else round(pe, 1),
                        None if pe is None else _band(pe / ref_pe if ref_pe else pe / 18, [(0.5, 100), (0.8, 80), (1, 60), (1.3, 35), (2, 10)]),
                        '倍', f"同{'產業' if market == 'tw' else '類股'}中位數 {ref_pe} 倍" if ref_pe else '沒有同業資料，以 18 倍為基準'))
    if fpe is not None:
        ref_f = (peer.get('fpe') if peer else None) or ref_pe
        v_metrics.append(_m('fpe', '預估本益比（未來 12 個月）', round(fpe, 1), _band(fpe / ref_f if ref_f else fpe / 16, [(0.5, 100), (0.8, 80), (1, 60), (1.3, 35), (2, 10)]),
                            '倍', '用分析師未來 12 個月 EPS 預估計算，市場看的是未來'))
    if own_pe_med and pe:
        v_metrics.append(_m('pe_hist', '相對自己過去本益比', round(pe / own_pe_med, 2), _band(pe / own_pe_med, [(0.6, 100), (0.85, 75), (1, 55), (1.25, 30), (1.6, 5)]),
                            '倍', f'近 {len(hist_pe)} 年本益比中位數 {own_pe_med} 倍；小於 1 代表比過去便宜'))
    if peg is not None:
        v_metrics.append(_m('peg', 'PEG（本益比 ÷ 成長率）', round(peg, 2), _band(peg, [(0.5, 100), (1, 75), (1.5, 50), (2.5, 15), (4, 0)]), '倍',
                            '彼得林區的指標：1 以下代表成長沒被高估'))
    if not financial:
        if ev_ebitda is not None:
            ref_e = peer.get('ev_ebitda') if peer else None
            v_metrics.append(_m('ev_ebitda', 'EV / EBITDA', round(ev_ebitda, 1), _band(ev_ebitda / ref_e if ref_e else ev_ebitda / 12, [(0.5, 100), (0.8, 80), (1, 60), (1.3, 35), (2, 10)]),
                                '倍', f'同類股中位數 {ref_e} 倍，含負債的整體估值' if ref_e else '含負債的整體估值，跨產業較好比較'))
        v_metrics.append(_m('fcf_yield', '自由現金流殖利率', _pct(fcf_yield), _band(fcf_yield, [(-0.02, 0), (0.01, 25), (0.04, 60), (0.07, 85), (0.1, 100)]), '%',
                            '買下整家公司，每年拿回的現金比例'))
    else:
        ref_pb = peer.get('pb') if peer else None
        v_metrics.append(_m('pb', '股價淨值比', None if pb is None else round(pb, 2), _band((pb or 0) / (ref_pb or 1.2), [(0.5, 100), (0.8, 80), (1, 60), (1.4, 30), (2, 5)]) if pb else None,
                            '倍', f'同產業中位數 {ref_pb} 倍' if ref_pb else '金融股的主要估值指標'))
    if dy is not None:
        ref_dy = peer.get('dy') if peer else None
        v_metrics.append(_m('dy', '現金殖利率', round(dy, 2), _band(dy, [(0, 20), (1.5, 45), (3, 65), (5, 90), (7, 100)]) if market == 'tw' else _band(dy, [(0, 40), (1.5, 55), (3, 70), (5, 85)]),
                            '%', (f'同產業中位數 {ref_dy}%' if ref_dy else '') + ('；台股投資人很重視配息' if market == 'tw' else '')))

    pillars = [
        {'key': 'growth', 'name': '成長性', 'weight': 25, 'metrics': g_metrics},
        {'key': 'profit', 'name': '獲利能力', 'weight': 20, 'metrics': p_metrics},
        {'key': 'health', 'name': '財務體質', 'weight': 15, 'metrics': h_metrics},
        {'key': 'quality', 'name': '盈餘品質與現金流', 'weight': 15, 'metrics': q_metrics},
        {'key': 'value', 'name': '估值', 'weight': 25, 'metrics': v_metrics},
    ]
    for p in pillars:
        p['score'] = _avg([m['score'] for m in p['metrics']])
    quality = _wavg([(p['score'], p['weight']) for p in pillars if p['key'] != 'value'])
    val_score = pillars[-1]['score']
    total = _wavg([(p['score'], p['weight']) for p in pillars])

    # ── 合理價 ──
    # DCF 用「正常化」自由現金流：景氣循環股今年特別差時改用近 3 年平均，太小（< 市值 1%）就不做 DCF
    fa3 = fcf_a.dropna().iloc[-3:]
    fcf_base = fcf_ttm
    if len(fa3) >= 2 and fa3.mean() > 0 and (fcf_ttm is None or fcf_ttm < fa3.mean()):
        fcf_base = float(fa3.mean())
    if fcf_base and mcap and fcf_base / mcap < 0.01:
        fcf_base = None
    fv = _fair_value(market, financial, price, eps_ttm, fwd_eps, g_for_peg, ref_pe, own_pe_med, fcf_base, shares,
                     roe, pb, info, n_est, net_debt)
    rec = _recommend(quality, val_score, fv, price, flags, financial)

    res = {
        'ticker': ticker, 'code': ticker.split('.')[0] if market == 'tw' else ticker, 'market': market,
        'name': tw_name or info.get('shortName') or info.get('longName') or ticker,
        'name_en': info.get('longName') or info.get('shortName'),
        'sector': tw_ind or sector, 'industry': info.get('industry'), 'currency': info.get('currency') or cur,
        'price': round(float(price), 2), 'mcap': mcap, 'financial': financial,
        'updated': pd.Timestamp.now(tz='Asia/Taipei').strftime('%Y-%m-%d %H:%M'),
        'latest_q': str(rev_q.dropna().index[-1].date()) if len(rev_q.dropna()) else None,
        'score': total, 'quality': quality, 'value_score': val_score, 'pillars': pillars,
        'fair': fv, 'rec': rec, 'flags': flags, 'peer': peer, 'tw_revenue': tw_rev,
        'analyst': _analyst(info, t, price, n_est),
        'charts': _charts(rev_a, gp_a, op_a, ni_a, eps_a, fcf_a, rev_q, gp_q, op_q, ni_q, eps_q, hist_pe),
        'fscore_parts': fparts,
        'summary': None,
    }
    res['summary'] = _narrative(res, rev_cagr, rev_yoy_q, fwd_growth, roe, om_trend, ocf_ni, nd_ebitda, pe, fpe, peg, dy)
    return _clean(res), None


def _wavg(pairs):
    ps = [(s, w) for s, w in pairs if s is not None]
    return round(sum(s * w for s, w in ps) / sum(w for _, w in ps)) if ps else None


def _tw_monthly(code):
    with tw_board._db() as con:
        rows = con.execute('SELECT ym, rev, rev_ly FROM revenue WHERE code=? ORDER BY ym DESC LIMIT 24', (code,)).fetchall()
    if len(rows) < 3:
        return None
    rows = rows[::-1]
    last3 = rows[-3:]
    yoy3 = (sum(r[1] for r in last3) / sum(r[2] for r in last3) - 1) * 100 if all(r[2] for r in last3) else None
    ytd = [r for r in rows if r[0][:4] == rows[-1][0][:4]]
    yoy_ytd = (sum(r[1] for r in ytd) / sum(r[2] for r in ytd) - 1) * 100 if ytd and all(r[2] for r in ytd) else None
    return {'last_ym': rows[-1][0], 'yoy3': None if yoy3 is None else round(yoy3, 1),
            'yoy_last': round((rows[-1][1] / rows[-1][2] - 1) * 100, 1) if rows[-1][2] else None,
            'yoy_ytd': None if yoy_ytd is None else round(yoy_ytd, 1),
            'months': [{'ym': r[0], 'rev': r[1], 'yoy': round((r[1] / r[2] - 1) * 100, 1) if r[2] else None} for r in rows[-13:]]}


def _hist_pe(t, eps_a):
    """每個會計年度底的股價 ÷ 當年 EPS，看這家公司過去通常被給幾倍。"""
    e = eps_a.dropna()
    if len(e) < 2:
        return []
    try:
        h = t.history(period='6y', interval='1mo', auto_adjust=False)['Close'].dropna()
    except Exception:
        return []
    if h.empty:
        return []
    h.index = h.index.tz_localize(None) if h.index.tz is not None else h.index
    out = []
    for d, eps in e.items():
        px = h[h.index <= pd.Timestamp(d) + pd.Timedelta(days=5)]
        if len(px) and eps > 0:
            out.append({'year': str(pd.Timestamp(d).date()), 'eps': round(float(eps), 2), 'price': round(float(px.iloc[-1]), 2),
                        'pe': round(float(px.iloc[-1] / eps), 1)})
    return out


def _piotroski(ni, ocf, ta, ltd, ca, cl, sh, gm, rev):
    """Piotroski F-score：比較最近兩個年度，九項各 1 分。"""
    try:
        def two(s):
            s = s.dropna()
            return (float(s.iloc[-1]), float(s.iloc[-2])) if len(s) >= 2 else (None, None)
        (n1, n0), (o1, _), (a1, a0) = two(ni), two(ocf), two(ta)
        if None in (n1, n0, o1, a1, a0):
            return None, []
        roa1, roa0 = n1 / a1, n0 / a0
        parts = [('ROA 為正', roa1 > 0), ('營業現金流為正', o1 > 0), ('ROA 比去年高', roa1 > roa0), ('現金流大於淨利', o1 > n1)]
        l1, l0 = two(ltd)
        parts.append(('長期負債比下降', (l1 or 0) / a1 <= (l0 or 0) / a0))
        c1, c0 = two(ca); d1, d0 = two(cl)
        parts.append(('流動比上升', bool(c1 and d1 and c0 and d0 and c1 / d1 > c0 / d0)))
        s1, s0 = two(sh)
        parts.append(('沒有增資稀釋', bool(s1 and s0 and s1 <= s0 * 1.01)))
        g1, g0 = two(gm)
        parts.append(('毛利率上升', bool(g1 is not None and g0 is not None and g1 > g0)))
        r1, r0 = two(rev)
        parts.append(('資產周轉率上升', bool(r1 and r0 and r1 / a1 > r0 / a0)))
        return sum(1 for _, ok in parts if ok), [{'name': n, 'ok': bool(ok)} for n, ok in parts]
    except Exception:
        return None, []


def _fair_value(market, financial, price, eps_ttm, fwd_eps, g, ref_pe, own_pe, fcf, shares, roe, pb, info, n_est, net_debt):
    methods = []
    r, tg = DISCOUNT[market], TERMINAL[market]
    g = max(-0.05, min(g if g is not None else 0.05, 0.3))
    # 1. 本益比法：合理本益比 = 同業與自己歷史的中位數，再依成長率微調；用未來 12 個月 EPS（沒有就用近四季 × (1+g)）
    base = [x for x in (ref_pe, own_pe) if x]
    eps_next = fwd_eps if fwd_eps and fwd_eps > 0 else (eps_ttm * (1 + g) if eps_ttm and eps_ttm > 0 else None)
    if base and eps_next:
        fpe = float(np.median(base))
        fpe *= 1 + max(-0.3, min((g - 0.08) * 1.5, 0.3))      # 成長高於 8% 給溢價，低於給折價
        fpe = max(6.0, min(fpe, 35.0))                         # 再好的公司也不給超過 35 倍，避免把熱度當價值
        methods.append({'name': '本益比法', 'value': eps_next * fpe,
                        'detail': f'未來 12 個月 EPS {eps_next:,.2f} × 合理本益比 {fpe:.1f} 倍（同業／自己過去中位數 {np.median(base):.1f} 倍，依成長調整）'})
    # 2. 兩段式 DCF：前 5 年照預估成長，第 6～10 年線性降到永續成長率，之後永續
    if not financial and fcf and fcf > 0 and shares:
        g_dcf = min(g, 0.25)
        pv, f = 0.0, fcf
        for yr in range(1, 11):
            gy = g_dcf if yr <= 5 else g_dcf + (tg - g_dcf) * (yr - 5) / 5
            f *= 1 + gy
            pv += f / (1 + r) ** yr
        tv = f * (1 + tg) / (r - tg) / (1 + r) ** 10
        eq_val = pv + tv - (net_debt or 0)
        if eq_val > 0:
            methods.append({'name': '現金流折現（DCF）', 'value': eq_val / shares,
                            'detail': f'自由現金流前 5 年成長 {g_dcf * 100:.0f}%、第 6～10 年降到 {tg * 100:.1f}%，折現率 {r * 100:.1f}%'})
    # 2'. 金融股：合理 PB = (ROE − g) / (r − g)
    if financial and roe and pb and roe > tg:
        bvps = price / pb
        fair_pb = max(0.4, min((roe - tg) / (r - tg), 4))
        methods.append({'name': 'ROE／股價淨值比法', 'value': bvps * fair_pb,
                        'detail': f'ROE {roe * 100:.1f}%、折現率 {r * 100:.1f}% → 合理股價淨值比 {fair_pb:.2f} 倍'})
    # 3. 分析師目標價（5 位以上才採用）
    tgt, n = info.get('targetMeanPrice'), info.get('numberOfAnalystOpinions') or n_est
    if tgt and n and n >= 5:
        methods.append({'name': '分析師目標價', 'value': float(tgt), 'detail': f'{n} 位分析師平均（12 個月目標價）'})
    if not methods:
        return None
    vals = [m['value'] for m in methods]
    fair = float(np.median(vals)) if len(vals) >= 3 else float(np.mean(vals))
    spread = max(vals) / min(vals) if min(vals) > 0 else 99
    conf = 'high' if len(vals) >= 3 and spread < 1.8 else 'low' if len(vals) < 2 or spread > 3 else 'mid'
    for m in methods:
        m['value'] = round(m['value'], 2)
        m['upside'] = round((m['value'] / price - 1) * 100, 1)
    return {'value': round(fair, 2), 'low': round(min(vals), 2), 'high': round(max(vals), 2),
            'upside': round((fair / price - 1) * 100, 1), 'buy_below': round(fair * (1 - SAFETY), 2), 'methods': methods,
            'confidence': conf, 'confidence_label': {'high': '高（三種方法接近）', 'mid': '中', 'low': '低（方法間差異大，僅供參考）'}[conf]}


def _recommend(quality, val_score, fv, price, flags, financial):
    up = fv['upside'] if fv else None
    q = quality or 0
    if flags and (len(flags) >= 2 or q < 50):
        level, why = 'avoid', '財務出現警訊，先避開，等體質改善再看'
    elif q >= 65 and up is not None and up >= 15:
        level, why = 'buy', '公司品質好，股價也比合理價低一段，有安全邊際'
    elif q >= 65 and up is not None and up >= 0:
        level, why = 'accumulate', '好公司但價格只是合理，建議分批、拉回到買進價以下再加碼'
    elif q >= 65 and up is not None and up < -30:
        level, why = 'hold', f'好公司，但股價比合理價高 {-up:.0f}%，不建議新買；持有者可分批獲利了結'
    elif q >= 65:
        level, why = 'hold', '好公司，但股價已經高於合理價，持有者續抱、新資金等回檔'
    elif up is not None and up < -30:
        level, why = 'avoid', f'股價比基本面估算的合理價高 {-up:.0f}%，估值過高'
    elif q >= 50 and up is not None and up >= 25 and (val_score or 0) >= 60:
        level, why = 'accumulate', '體質普通但估值明顯偏低，屬價值型機會，部位不宜大'
    elif q >= 50:
        level, why = 'hold', '體質中等、價格沒有特別便宜，觀望就好'
    else:
        level, why = 'avoid', '成長、獲利或現金流偏弱，目前不建議買進'
    if flags and level in ('buy', 'accumulate'):
        level, why = 'hold', why + '；但有財務警訊，先降為觀望'
    label = {'buy': '買進', 'accumulate': '逢低布局', 'hold': '持有觀望', 'avoid': '避開'}[level]
    return {'level': level, 'label': label, 'why': why}


def _analyst(info, t, price, n_est):
    out = {'key': info.get('recommendationKey'), 'mean': info.get('recommendationMean'),
           'n': info.get('numberOfAnalystOpinions') or n_est, 'target': info.get('targetMeanPrice'),
           'low': info.get('targetLowPrice'), 'high': info.get('targetHighPrice')}
    if out['target']:
        out['upside'] = round((out['target'] / price - 1) * 100, 1)
    try:
        rs = t.recommendations_summary
        if rs is not None and not rs.empty:
            r0 = rs.iloc[0]
            out['dist'] = {k: int(r0.get(k, 0) or 0) for k in ('strongBuy', 'buy', 'hold', 'sell', 'strongSell')}
    except Exception:
        pass
    out['label'] = {'strong_buy': '強力買進', 'buy': '買進', 'hold': '持有', 'underperform': '表現落後', 'sell': '賣出',
                    'none': '—'}.get(out['key'] or 'none', out['key'])
    return out


def _charts(rev_a, gp_a, op_a, ni_a, eps_a, fcf_a, rev_q, gp_q, op_q, ni_q, eps_q, hist_pe):
    def ser(s):
        s = s.dropna()
        return [[str(pd.Timestamp(d).date()), float(v)] for d, v in s.items()]

    def margin(a, b):
        m = (a / b).dropna()
        return [[str(pd.Timestamp(d).date()), round(float(v) * 100, 1)] for d, v in m.items()]
    return {
        'annual': {'rev': ser(rev_a), 'ni': ser(ni_a), 'eps': ser(eps_a), 'fcf': ser(fcf_a),
                   'gm': margin(gp_a, rev_a), 'om': margin(op_a, rev_a), 'nm': margin(ni_a, rev_a)},
        'quarterly': {'rev': ser(rev_q), 'ni': ser(ni_q), 'eps': ser(eps_q),
                      'gm': margin(gp_q, rev_q), 'om': margin(op_q, rev_q), 'nm': margin(ni_q, rev_q)},
        'pe_hist': hist_pe,
    }


def _narrative(r, rev_cagr, rev_yoy_q, fwd_g, roe, om_trend, ocf_ni, nd_ebitda, pe, fpe, peg, dy):
    """像研究報告的投資論點：亮點、隱憂、結論。只引用算得出來的數字。"""
    pros, cons = [], []
    if rev_yoy_q is not None:
        (pros if rev_yoy_q > 0.1 else cons if rev_yoy_q < 0 else pros).append(
            f"最新一季營收年增 {rev_yoy_q * 100:+.1f}%" + ('，成長動能強' if rev_yoy_q > 0.2 else '' if rev_yoy_q >= 0 else '，營收衰退'))
    if rev_cagr is not None and abs(rev_cagr) > 0.05:
        (pros if rev_cagr > 0 else cons).append(f'營收三年複合成長 {rev_cagr * 100:+.1f}%')
    if fwd_g is not None:
        (pros if fwd_g > 0.08 else cons if fwd_g < 0 else pros).append(f'分析師預估未來 12 個月 EPS 成長 {fwd_g * 100:+.1f}%')
    tr = r.get('tw_revenue')
    if tr and tr.get('yoy3') is not None:
        (pros if tr['yoy3'] > 5 else cons if tr['yoy3'] < -5 else pros).append(f"近 3 個月營收年增 {tr['yoy3']:+.1f}%（至 {tr['last_ym']}）")
    if roe is not None:
        if roe >= 0.15:
            pros.append(f'ROE {roe * 100:.1f}%，資本運用效率佳')
        elif roe < 0.08:
            cons.append(f'ROE 只有 {roe * 100:.1f}%，低於資金成本')
    if om_trend is not None and abs(om_trend) >= 0.02:
        (pros if om_trend > 0 else cons).append(f"營益率 {'擴張' if om_trend > 0 else '下滑'} {abs(om_trend) * 100:.1f} 個百分點")
    if ocf_ni is not None:
        if ocf_ni >= 1.1:
            pros.append(f'營業現金流是淨利的 {ocf_ni:.1f} 倍，盈餘品質好')
        elif ocf_ni < 0.8:
            cons.append(f'營業現金流只有淨利的 {ocf_ni:.0%}')
    if nd_ebitda is not None:
        if nd_ebitda < 0:
            pros.append('帳上淨現金，財務穩健')
        elif nd_ebitda > 3:
            cons.append(f'淨負債 / EBITDA {nd_ebitda:.1f} 倍，槓桿偏高')
    if peg is not None:
        (pros if peg < 1 else cons if peg > 2 else pros).append(f'PEG {peg:.2f}' + ('，以成長來看不貴' if peg < 1.2 else '，成長已反映在股價' if peg > 2 else ''))
    peer = r.get('peer') or {}
    if pe and peer.get('pe'):
        rel = pe / peer['pe']
        if rel < 0.85:
            pros.append(f"本益比 {pe:.1f} 倍，低於同業中位數 {peer['pe']} 倍")
        elif rel > 1.3:
            cons.append(f"本益比 {pe:.1f} 倍，高於同業中位數 {peer['pe']} 倍")
    if dy is not None and r['market'] == 'tw' and dy >= 4:
        pros.append(f'殖利率 {dy:.1f}%，配息具吸引力')
    cons += [f for f in r['flags'] if f not in cons]
    # 研究報告一定要寫風險：再從分數偏低的指標補上
    for p in r['pillars']:
        for m in p['metrics']:
            base = re.split(r'[（ ]', m['label'])[0]
            if m['score'] is not None and m['score'] < 40 and len(cons) < 6 and not any(base in c for c in cons):
                v = m['value'] if not isinstance(m['value'], float) else round(m['value'], 2)
                unit = '' if not m['fmt'] or isinstance(v, str) else m['fmt'] if m['fmt'] == '%' else ' ' + m['fmt']
                cons.append(f"{m['label']} {v}{unit}，偏弱")
    if not cons:
        cons.append('財報數字沒有明顯弱點；主要風險在產業景氣反轉與估值修正，'
                    + ('股價已不便宜' if (r.get('value_score') or 0) < 60 else '留意成長是否放緩'))
    fv, rec = r.get('fair'), r['rec']
    cur = r.get('currency') or ''
    if fv:
        concl = (f"綜合三種估值方法，合理價約 {fv['value']:,.2f} {cur}（區間 {fv['low']:,.2f}～{fv['high']:,.2f}），"
                 f"現價 {r['price']:,.2f}，{'潛在上漲' if fv['upside'] >= 0 else '高於合理價'} {abs(fv['upside']):.1f}%。"
                 f"建議「{rec['label']}」：{rec['why']}。"
                 + (f"買進價參考 {fv['buy_below']:,.2f} 以下（保留 {int(SAFETY * 100)}% 安全邊際）。" if rec['level'] in ('buy', 'accumulate', 'hold') else ''))
    else:
        concl = f"資料不足以估算合理價。建議「{rec['label']}」：{rec['why']}。"
    return {'pros': pros[:6], 'cons': cons[:6], 'conclusion': concl}


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if np.isnan(o) or np.isinf(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/financials/<code>')
def api_financials(code):
    try:
        res, err = analyze(code)
    except Exception as e:
        if 'Too Many Requests' in str(e):
            return jsonify(error='Yahoo 暫時限制查詢次數，請過幾分鐘再試'), 503
        return jsonify(error=f'分析失敗：{e}'), 503
    if err:
        return jsonify(error=err), 404
    return jsonify(res)


def init(app, login_required):
    @app.route('/financials')
    @login_required
    def financials_page():
        return render_template('financials.html')

    app.register_blueprint(bp)
