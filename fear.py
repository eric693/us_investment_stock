"""恐慌指標：儀表板、「VIX 飆高加碼」回測、加碼提醒。

指標（Yahoo Finance，美股收盤後更新）：VIX、短天期 VIX9D、三個月 VIX3M、VVIX（VIX 的波動）、
VXN（那斯達克）、MOVE（美債）、SKEW（黑天鵝）、OVX（原油）、GVZ（黃金）；另加 CNN 恐懼貪婪指數。
每個指標給出現值、和過去 10 年比的百分位，以及怎麼解讀。

回測（VIX 從 1990 年起）三種加碼時機，對照之後 1／3／6／12 個月的報酬：
1. 觸及：VIX 收盤首次站上門檻（前 20 個交易日都在門檻下，同一波恐慌只算一次）。
2. 回落：觸及門檻後，VIX 從這波高點回落 20% 的第一天——恐慌開始退燒才進場。
3. 期限結構恢復：VIX 高於 VIX3M（短期恐慌大於長期，倒掛）連續 3 天以上後，第一天恢復正常（2006 年起）。
進場價：S&P 500 用訊號隔一個交易日的收盤；台股加權用訊號日之後的第一個台股交易日收盤
（VIX 在美股收盤才確定，台股已經收盤了）。指數只算價格、不含股息，和「任何一天買進」的平均比較。
"""
import os
import time
import json
import pickle
import fcntl
import threading

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from flask import Blueprint, jsonify, request, render_template

import notify
import userdata
import tw_board

bp = Blueprint('fear', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, '.fear_cache')
LOCK_FILE = os.path.join(BASE_DIR, '.fear_alert.lock')
SITE_URL = os.environ.get('NOTIFY_SITE_URL', notify.VAPID_SUB)
HORIZONS = [(21, '1 個月'), (63, '3 個月'), (126, '6 個月'), (252, '12 個月')]
THRESHOLDS = [20, 25, 30, 40]
ALERT_KEY, ALERT_STATE = 'fear_alerts', 'fear_alerts_state'

INDICATORS = [
    ('^VIX', 'VIX 恐慌指數', 'S&P 500 未來 30 天的預期波動。20 以下平靜、25～30 恐懼、30 以上恐慌、40 以上崩盤級'),
    ('^VIX9D', 'VIX9D 短天期', '未來 9 天的預期波動；比 VIX 高代表眼前的恐慌特別強'),
    ('^VIX3M', 'VIX3M 三個月', '未來 3 個月的預期波動；正常時比 VIX 高'),
    ('^VVIX', 'VVIX', 'VIX 本身的波動；超過 120～130 代表恐慌正在升溫、還不穩定'),
    ('^VXN', 'VXN 那斯達克', '科技股的恐慌程度'),
    ('^MOVE', 'MOVE 美債波動', '債市的 VIX；股債同時恐慌時，往往是資金面出問題'),
    ('^SKEW', 'SKEW 黑天鵝', '機構買深度價外保護的程度；偏高代表擔心暴跌，但不是時機訊號'),
    ('^OVX', 'OVX 原油波動', '原油的恐慌指數'),
    ('^GVZ', 'GVZ 黃金波動', '黃金的恐慌指數'),
]
_mem, _lock = {}, threading.Lock()


def _hist(sym):
    """日收盤（全部歷史），硬碟快取 6 小時。"""
    with _lock:
        hit = _mem.get(sym)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, sym.replace('^', '_') + '.pkl')
    s = None
    try:
        with open(path, 'rb') as f:
            ts, s = pickle.load(f)
        if time.time() - ts > 6 * 3600:
            raise ValueError('stale')
    except Exception:
        try:
            h = yf.Ticker(sym).history(period='max', interval='1d', auto_adjust=False)
            if len(h):
                s = h['Close'].dropna()
                s.index = pd.to_datetime(s.index.tz_localize(None) if s.index.tz is not None else s.index).normalize()
                with open(path + '.tmp', 'wb') as f:
                    pickle.dump((time.time(), s), f)
                os.replace(path + '.tmp', path)
        except Exception as e:
            print(f'[fear] {sym}: {e}')
            if s is None:
                raise
    with _lock:
        _mem[sym] = (time.time(), s)
    return s


def vix_label(v):
    if v is None:
        return '—', 'neutral-m'
    if v < 13:
        return '過度樂觀', 'greed-hi'
    if v < 20:
        return '平靜', 'greed'
    if v < 25:
        return '警戒', 'neutral-m'
    if v < 30:
        return '恐懼', 'fear'
    return '極度恐懼', 'fear-hi'


def _cnn():
    def fetch():
        r = requests.get('https://production.dataviz.cnn.io/index/fearandgreed/graphdata', timeout=15,
                         headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36'})
        j = r.json()
        fg = j['fear_and_greed']
        hist = [[pd.Timestamp(p['x'], unit='ms').strftime('%Y-%m-%d'), round(p['y'], 1)] for p in j.get('fear_and_greed_historical', {}).get('data', [])]
        zh = {'extreme fear': '極度恐懼', 'fear': '恐懼', 'neutral': '中性', 'greed': '貪婪', 'extreme greed': '極度貪婪'}
        return {'score': round(fg['score'], 1), 'rating': zh.get(fg['rating'], fg['rating']),
                'prev': round(fg['previous_close'], 1), 'week': round(fg['previous_1_week'], 1),
                'month': round(fg['previous_1_month'], 1), 'year': round(fg['previous_1_year'], 1),
                'updated': fg['timestamp'][:16].replace('T', ' '), 'history': hist[-260:]}
    with _lock:
        hit = _mem.get('cnn')
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
    try:
        v = fetch()
    except Exception as e:
        print(f'[fear] cnn: {e}')
        v = hit[1] if hit else None
    with _lock:
        _mem['cnn'] = (time.time(), v)
    return v


def dashboard():
    out = []
    cut10 = pd.Timestamp.now() - pd.Timedelta(days=3650)
    for sym, name, note in INDICATORS:
        try:
            s = _hist(sym)
        except Exception:
            s = None
        if s is None or len(s) < 30:
            out.append({'sym': sym, 'name': name, 'note': note, 'value': None})
            continue
        last, prev = float(s.iloc[-1]), float(s.iloc[-2])
        ten = s[s.index >= cut10]
        out.append({'sym': sym, 'name': name, 'note': note, 'value': round(last, 2), 'date': str(s.index[-1].date()),
                    'chg': round((last / prev - 1) * 100, 2), 'pct10y': round(float((ten < last).mean() * 100), 0),
                    'med10y': round(float(ten.median()), 2), 'hi1y': round(float(s.iloc[-252:].max()), 2), 'lo1y': round(float(s.iloc[-252:].min()), 2)})
    vix, v3m, v9d = _hist('^VIX'), _hist('^VIX3M'), _hist('^VIX9D')
    two = vix.index[-1] - pd.Timedelta(days=730)
    ratio = (vix / v3m).dropna()
    term = {'ratio': round(float(ratio.iloc[-1]), 3), 'inverted': bool(ratio.iloc[-1] > 1),
            'ratio9d': round(float((v9d / vix).dropna().iloc[-1]), 3),
            'inverted_days_1y': int((ratio.iloc[-252:] > 1).sum())}
    lbl, cls = vix_label(float(vix.iloc[-1]))
    cnn, sig = _cnn(), current_signal()
    # CNN 綜合了動能、新高新低家數、垃圾債利差等，常比 VIX 早反映情緒；兩者不一致時一起說明
    if cnn and cnn['score'] <= 25 and sig['vix'] < 25:
        sig = dict(sig, text=f"CNN 恐懼貪婪指數 {cnn['score']:.0f}（{cnn['rating']}），市場情緒偏悲觀；但 VIX 只有 {sig['vix']:.1f}，"
                             '屬一般修正而非恐慌。可小量分批，回測顯示 VIX 40 以上或期限結構恢復時才是較可靠的大舉加碼點')
    elif cnn and cnn['score'] >= 75 and sig['vix'] < 15:
        sig = dict(sig, text=f"CNN 恐懼貪婪指數 {cnn['score']:.0f}（{cnn['rating']}）且 VIX 偏低，市場樂觀，不宜追高加碼")
    return {'indicators': out, 'term': term, 'vix_label': lbl, 'vix_cls': cls, 'cnn': cnn,
            'chart': {'vix': [[str(d.date()), round(float(v), 2)] for d, v in vix[vix.index >= two].items()],
                      'ratio': [[str(d.date()), round(float(v), 3)] for d, v in ratio[ratio.index >= two].items()]},
            'signal': sig}


# ── 回測 ──────────────────────────────────────────────────────────────
def _events(vix, thr, rule, v3m=None):
    """回傳 [(訊號日, 當時 VIX, 這波高點)]。"""
    v = vix.values
    idx = vix.index
    out = []
    if rule == 'term':
        r = (vix / v3m).dropna()
        rv, ri = r.values, r.index
        inv = 0
        last = None
        for i in range(1, len(rv)):
            if rv[i] > 1:
                inv += 1
                continue
            if inv >= 3 and (last is None or i - last > 20):
                d = ri[i]
                out.append((d, float(vix.loc[d]), float(vix.loc[ri[i - inv]:d].max())))
                last = i
            inv = 0
        return out
    i, n = 20, len(v)
    while i < n:
        if v[i] >= thr and (v[i - 20:i] < thr).all():
            if rule == 'touch':
                out.append((idx[i], float(v[i]), float(v[i])))
                i += 20
                continue
            peak, j = v[i], i
            while j < min(n, i + 120):
                peak = max(peak, v[j])
                if v[j] <= peak * 0.8:
                    out.append((idx[j], float(v[j]), float(peak)))
                    break
                j += 1
            i = j + 20
            continue
        i += 1
    return out


def backtest(market='spx'):
    vix, v3m = _hist('^VIX'), _hist('^VIX3M')
    px = _hist('^GSPC' if market == 'spx' else '^TWII')
    px = px[px.index >= pd.Timestamp('1990-01-01')]
    pv, pidx = px.values, px.index

    def entry(d):
        # S&P：隔一個交易日收盤；台股：訊號日之後第一個台股交易日
        k = pidx.searchsorted(d, side='right')
        if market == 'spx':
            k = k if k < len(pidx) else None
        return k if k is not None and k < len(pidx) else None
    # 基準：任何一天買進的平均
    base = {}
    for h, _ in HORIZONS:
        r = pv[h:] / pv[:-h] - 1
        base[h] = {'ret': round(float(r.mean()) * 100, 2), 'win': round(float((r > 0).mean()) * 100, 1)}
    rules = [('touch', '觸及門檻就買'), ('retreat', '從高點回落 20% 才買')]
    table, events_out = [], {}
    for rule, rname in rules + [('term', '期限結構恢復正常才買')]:
        for thr in (THRESHOLDS if rule != 'term' else [None]):
            evs = _events(vix, thr, rule, v3m)
            rows, cells = [], []
            for d, vv, peak in evs:
                k = entry(d)
                if k is None:
                    continue
                e = {'date': str(d.date()), 'vix': round(vv, 1), 'peak': round(peak, 1), 'entry': str(pidx[k].date()),
                     'price': round(float(pv[k]), 2)}
                for h, _ in HORIZONS:
                    e[f'r{h}'] = round((pv[k + h] / pv[k] - 1) * 100, 1) if k + h < len(pv) else None
                win = pv[k:k + 63]
                e['mdd3m'] = round((win.min() / pv[k] - 1) * 100, 1) if len(win) else None
                rows.append(e)
            for h, hn in HORIZONS:
                rs = [r[f'r{h}'] for r in rows if r[f'r{h}'] is not None]
                cells.append({'h': hn, 'n': len(rs), 'ret': round(float(np.mean(rs)), 2) if rs else None,
                              'win': round(float(np.mean(np.array(rs) > 0)) * 100, 1) if rs else None,
                              'excess': round(float(np.mean(rs)) - base[h]['ret'], 2) if rs else None,
                              'worst': round(float(min(rs)), 1) if rs else None})
            mdds = [r['mdd3m'] for r in rows if r['mdd3m'] is not None]
            key = f'{rule}_{thr}' if thr else rule
            table.append({'key': key, 'rule': rule, 'rule_name': rname, 'thr': thr, 'n': len(rows), 'cells': cells,
                          'mdd3m': round(float(np.mean(mdds)), 1) if mdds else None})
            events_out[key] = rows[::-1]
    return {'market': market, 'index': 'S&P 500' if market == 'spx' else '台股加權指數',
            'since': str(max(vix.index[0], pidx[0]).date()), 'base': [{'h': hn, **base[h]} for h, hn in HORIZONS],
            'table': table, 'events': events_out}


_bt_cache = {}


def backtest_cached(market):
    hit = _bt_cache.get(market)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    res = backtest(market)
    _bt_cache[market] = (time.time(), res)
    return res


# ── 目前訊號與提醒 ────────────────────────────────────────────────────
def current_signal():
    vix, v3m = _hist('^VIX'), _hist('^VIX3M')
    v = float(vix.iloc[-1])
    win = vix.iloc[-60:]
    peak, peak_date = float(win.max()), str(win.idxmax().date())
    above = [t for t in THRESHOLDS if v >= t]
    ratio = (vix / v3m).dropna()
    inv_days = 0
    for x in ratio.values[::-1][1:]:
        if x > 1:
            inv_days += 1
        else:
            break
    out = {'vix': round(v, 2), 'peak60': round(peak, 2), 'peak_date': peak_date, 'from_peak': round((v / peak - 1) * 100, 1),
           'above': above, 'retreat': bool(peak >= 25 and v <= peak * 0.8),
           'term_normalized': bool(ratio.iloc[-1] < 1 and inv_days >= 3)}
    if out['retreat']:
        out['text'] = f'VIX 從近 60 日高點 {peak:.1f} 回落 {-out["from_peak"]:.0f}%，恐慌退燒中，歷史上是較好的分批加碼時機'
    elif out['term_normalized']:
        out['text'] = '期限結構剛從倒掛恢復正常，恐慌高峰可能已過'
    elif v >= 30:
        out['text'] = f'VIX {v:.1f}，市場恐慌；可開始分批，但別一次重壓，等回落更穩'
    elif v >= 25:
        out['text'] = f'VIX {v:.1f}，恐懼升溫，留意是否繼續擴大'
    else:
        out['text'] = f'VIX {v:.1f}，沒有明顯恐慌，照原本的計畫定期定額即可'
    return out


def alert_cfg(user):
    c = userdata.get(user, ALERT_KEY) or {}
    thr = [t for t in (c.get('thresholds') or []) if t in THRESHOLDS]
    return {'thresholds': thr, 'retreat': bool(c.get('retreat')), 'term': bool(c.get('term'))}


def run_alerts():
    vix = _hist('^VIX')
    sig = current_signal()
    v, day = sig['vix'], str(vix.index[-1].date())
    prev = vix.iloc[-21:-1]
    for user, _ in userdata.users_with(ALERT_KEY):
        cfg = alert_cfg(user)
        st = userdata.get(user, ALERT_STATE) or {}
        msgs = []
        for t in cfg['thresholds']:
            k = f'thr{t}'
            # 前 20 天都在門檻下、今天站上 → 同一波只通知一次
            if v >= t and (prev < t).all() and st.get(k) != day:
                st[k] = day
                msgs.append(f'VIX 站上 {t}（目前 {v:.1f}），市場恐慌升溫')
        if cfg['retreat'] and sig['retreat'] and st.get('retreat_peak') != sig['peak_date']:
            st['retreat_peak'] = sig['peak_date']              # 每一波高點只通知一次
            msgs.append(sig['text'])
        if cfg['term'] and sig['term_normalized'] and st.get('term') != day:
            st['term'] = day
            msgs.append('VIX 期限結構從倒掛恢復正常，恐慌高峰可能已過')
        if msgs:
            userdata.put(user, ALERT_STATE, st)
            if notify.ready(user):
                notify.send(user, '【StockLens 恐慌指標】\n' + '\n'.join(msgs) + f'\n回測與現況：{SITE_URL}/fear', url='/fear')


def _alert_loop():
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    time.sleep(180)
    while True:
        try:
            if userdata.users_with(ALERT_KEY):
                with _lock:
                    _mem.pop('^VIX', None)          # 提醒要用最新的 VIX
                run_alerts()
        except Exception as e:
            print(f'[fear] alerts: {e}')
        time.sleep(1800)


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/fear')
def api_dashboard():
    try:
        return jsonify(dashboard())
    except Exception as e:
        msg = 'Yahoo 暫時限制查詢次數，請過幾分鐘再試' if 'Too Many' in str(e) else f'資料讀取失敗：{e}'
        return jsonify(error=msg), 503


@bp.route('/api/fear/backtest')
def api_backtest():
    m = request.args.get('market', 'spx')
    if m not in ('spx', 'twii'):
        return jsonify(error='market 必須是 spx 或 twii'), 400
    try:
        return jsonify(backtest_cached(m))
    except Exception as e:
        return jsonify(error=f'回測失敗：{e}'), 503


@bp.route('/api/fear/alerts', methods=['GET', 'POST'])
def api_alerts():
    user = userdata.current()
    if request.method == 'POST':
        b = request.get_json(silent=True) or {}
        thr = [t for t in (b.get('thresholds') or []) if t in THRESHOLDS]
        userdata.put(user, ALERT_KEY, {'thresholds': sorted(set(thr)), 'retreat': bool(b.get('retreat')), 'term': bool(b.get('term'))})
    return jsonify(dict(alert_cfg(user), ready=notify.ready(user)))


def init(app, login_required):
    @app.route('/fear')
    @login_required
    def fear_page():
        return render_template('fear.html')

    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_alert_loop, daemon=True).start()
