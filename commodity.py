"""黃金、原油：趨勢回檔進場 + ATR 移動停損出場，含歷史回測。

規則（只做多；做空在回測中黃金、原油幾乎每個週期都虧，所以不用）：
1. 趨勢：EMA50 在 EMA200 之上，且收盤在 EMA200 之上。
2. 回檔：最近 5 根 K 棒內，最低價碰到 EMA20。
3. 進場訊號：收盤重新站上 EMA20，且突破前一根 K 棒高點 → 下一根開盤進場。
4. 初始停損：最近 5 根最低點再往下 0.5 ATR，距離限制在 1～3 ATR 之間。
5. 移動停損：進場後最高價 − 3 ATR，只升不降；價格碰到就出場（跳空則以開盤價）。
   這條停損線就是圖上的出場橫線。2R（兩倍初始風險）只當參考目標，不會提前出場。

參數刻意不最佳化（3 ATR 是 2.5／3／4 三組中間的那組），頁面同時列出前後兩段的成績看穩定度。
資料：Yahoo 期貨連續月 GC=F、CL=F。1 小時約兩年；4 小時由 1 小時合成；日線十年。
"""
import os
import time
import fcntl
import threading

import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, jsonify, request, render_template

import notify
import userdata

bp = Blueprint('commodity', __name__)

SYMBOLS = {
    'gold': {'ticker': 'GC=F', 'name': '黃金', 'unit': '美元／盎司', 'cost': 0.0003},
    'oil': {'ticker': 'CL=F', 'name': '原油 WTI', 'unit': '美元／桶', 'cost': 0.0005},
}
TFS = {'1h': '1 小時', '4h': '4 小時', '1d': '日線'}
RULE = {'fast': 20, 'mid': 50, 'slow': 200, 'atr': 14, 'look': 5, 'trail': 3.0}
TZ_SHIFT = 8 * 3600                       # 圖上時間顯示台北時間
TTL = {'1h': 600, '4h': 600, '1d': 3600}
CHART_BARS = {'1h': 3000, '4h': 4000, '1d': 2600}   # 圖上畫的根數（回測仍用全部資料）

_cache = {}
_lock = threading.Lock()


def _download(ticker, tf):
    if tf == '1d':
        h = yf.Ticker(ticker).history(period='10y', interval='1d', auto_adjust=False)
    else:
        h = yf.Ticker(ticker).history(period='730d', interval='60m', auto_adjust=False)
        if tf == '4h':
            # 期貨交易日從紐約 18:00 開始，用 18/22/2/6/10/14 點切
            h = h.resample('4h', offset='2h').agg(
                {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
    df = h[['Open', 'High', 'Low', 'Close']].dropna()
    df = df[df['Low'] > 0]                # 2020-04 原油負價，百分比算不了
    if ticker == 'CL=F' and tf == '1d':
        df = df[df.index >= '2020-07-01']
    return df


def _ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def _atr(H, L, C, n):
    pc = np.r_[C[0], C[:-1]]
    tr = np.maximum(H - L, np.maximum(abs(H - pc), abs(L - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values


def backtest(df, cost=0.0, rule=RULE):
    """回傳 (trades, state)。trades 每筆含進出場位置、價格與每根 K 棒的停損價（畫出場線用）。"""
    O, H, L, C = (df[c].values for c in ('Open', 'High', 'Low', 'Close'))
    e_f, e_m, e_s = _ema(C, rule['fast']), _ema(C, rule['mid']), _ema(C, rule['slow'])
    atr = _atr(H, L, C, rule['atr'])
    n, look, k = len(C), rule['look'], rule['trail']
    trades = []
    i = rule['slow']
    while i < n - 1:
        lo = i - look + 1
        trend = e_m[i] > e_s[i] and C[i] > e_s[i]
        if not (trend and L[lo:i + 1].min() <= e_f[i] and C[i] > e_f[i] and C[i] > H[i - 1]):
            i += 1
            continue
        ent, a = O[i + 1], atr[i]
        stop = min(L[lo:i + 1].min() - 0.5 * a, ent - a)
        stop = max(stop, ent - 3 * a)
        risk = ent - stop
        t = {'signal': i, 'entry_i': i + 1, 'entry': ent, 'stop0': stop, 'risk': risk,
             'target': ent + 2 * risk, 'stops': []}
        peak, j = ent, i + 1
        while j < n:
            t['stops'].append(stop)
            if L[j] <= stop:
                t.update(exit_i=j, exit=min(O[j], stop), open=False)
                break
            peak = max(peak, H[j])
            stop = max(stop, peak - k * atr[j])
            j += 1
        else:
            t.update(exit_i=n - 1, exit=C[-1], open=True, stop_now=stop)
        t['ret'] = (t['exit'] - ent) / ent - (0 if t['open'] else cost)
        t['R'] = (t['exit'] - ent) / risk
        trades.append(t)
        if t['open']:
            break
        i = t['exit_i'] + 1

    last = n - 1
    trend = e_m[last] > e_s[last] and C[last] > e_s[last]
    if trades and trades[-1]['open']:
        state = 'holding'
    elif not trend:
        state = 'no_trend'
    elif L[last - look + 1:last + 1].min() <= e_f[last]:
        state = 'pullback'                # 已回檔，等站回 EMA20 並突破前高
    else:
        state = 'waiting'                 # 趨勢向上但離 EMA20 遠，等回檔
    ind = {'ema_fast': e_f, 'ema_mid': e_m, 'ema_slow': e_s, 'atr': atr}
    return trades, {'state': state, 'ind': ind}


def _stats(trades, df, start_i):
    closed = [t for t in trades if not t['open']]
    rets = np.array([t['ret'] for t in closed])
    C = df['Close'].values
    bh = (C[-1] / C[start_i] - 1) * 100 if start_i < len(C) else None
    if not len(rets):
        return {'n': 0, 'bh': round(bh, 1) if bh is not None else None}
    eq = np.cumprod(1 + rets)
    held = sum(t['exit_i'] - t['entry_i'] + 1 for t in trades)
    span = len(C) - start_i
    return {
        'n': len(rets), 'win': round(float((rets > 0).mean() * 100), 1),
        'avg_win': round(float(rets[rets > 0].mean() * 100), 2) if (rets > 0).any() else 0,
        'avg_loss': round(float(rets[rets <= 0].mean() * 100), 2) if (rets <= 0).any() else 0,
        'avg_R': round(float(np.mean([t['R'] for t in closed])), 2),
        'total': round(float((eq[-1] - 1) * 100), 1),
        'max_dd': round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 1),
        'pf': round(float(rets[rets > 0].sum() / -rets[rets < 0].sum()), 2) if (rets < 0).any() else None,
        'exposure': round(held / span * 100, 1) if span else 0,
        'bh': round(bh, 1) if bh is not None else None,
    }


def _times(idx, tf):
    if tf == '1d':
        return [d.strftime('%Y-%m-%d') for d in idx]
    return [int(t.timestamp()) + TZ_SHIFT for t in idx]


def _label(idx, i, tf):
    t = idx[i].tz_convert('Asia/Taipei') if idx.tz is not None else idx[i]
    return t.strftime('%Y-%m-%d') if tf == '1d' else t.strftime('%Y-%m-%d %H:%M')


def analyze(key, tf):
    sym = SYMBOLS[key]
    ck = (key, tf)
    with _lock:
        hit = _cache.get(ck)
        if hit and time.time() - hit[0] < TTL[tf]:
            return hit[1]
    df = _download(sym['ticker'], tf)
    if len(df) < RULE['slow'] + 50:
        return None
    trades, st = backtest(df, sym['cost'])
    times, idx = _times(df.index, tf), df.index
    half = (RULE['slow'] + len(df)) // 2
    split_t = _label(idx, half, tf)
    r2 = lambda v: round(float(v), 4 if v < 10 else 2)

    view = max(0, len(df) - CHART_BARS[tf])
    out_trades = []
    for t in trades:
        out_trades.append({
            'entry_time': times[t['entry_i']], 'exit_time': times[t['exit_i']],
            'entry_label': _label(idx, t['entry_i'], tf), 'exit_label': _label(idx, t['exit_i'], tf),
            'entry': r2(t['entry']), 'exit': r2(t['exit']), 'stop0': r2(t['stop0']), 'target': r2(t['target']),
            'ret': round(t['ret'] * 100, 2), 'R': round(t['R'], 2), 'open': t['open'],
            'bars': t['exit_i'] - t['entry_i'] + 1,
            'stops': [[times[t['entry_i'] + k], r2(s)] for k, s in enumerate(t['stops'])
                      if t['entry_i'] + k >= view],
            'in_view': t['exit_i'] >= view,
        })
    cur = None
    if trades and trades[-1]['open']:
        t = trades[-1]
        cur = {'entry': r2(t['entry']), 'since': _label(idx, t['entry_i'], tf), 'stop': r2(t['stop_now']),
               'target': r2(t['target']), 'pnl': round(t['ret'] * 100, 2), 'R': round(t['R'], 2)}

    ind = st['ind']
    keep = lambda arr: [[times[i], r2(v)] for i, v in enumerate(arr) if i >= max(view, RULE['slow'] // 4)]
    C = df['Close'].values
    res = {
        'key': key, 'name': sym['name'], 'ticker': sym['ticker'], 'unit': sym['unit'], 'tf': tf, 'tf_name': TFS[tf],
        'updated': pd.Timestamp.now(tz='Asia/Taipei').strftime('%Y-%m-%d %H:%M'),
        'last': {'time': _label(idx, len(df) - 1, tf), 'close': r2(C[-1]),
                 'chg': round((C[-1] / C[-2] - 1) * 100, 2), 'atr': r2(ind['atr'][-1])},
        'bars': [[times[i], r2(o), r2(h), r2(l), r2(c)] for i, (o, h, l, c) in
                 enumerate(df[['Open', 'High', 'Low', 'Close']].itertuples(index=False)) if i >= view],
        'ema': {'20': keep(ind['ema_fast']), '50': keep(ind['ema_mid']), '200': keep(ind['ema_slow'])},
        'trades': out_trades, 'state': st['state'], 'position': cur,
        'stats': {
            'all': _stats(trades, df, RULE['slow']),
            'first': _stats([t for t in trades if t['entry_i'] < half], df.iloc[:half + 1], RULE['slow']),
            'second': _stats([t for t in trades if t['entry_i'] >= half], df, half),
        },
        'range': [_label(idx, RULE['slow'], tf), _label(idx, len(df) - 1, tf)], 'split': split_t,
        'rule': RULE, 'cost': sym['cost'] * 100,
    }
    with _lock:
        _cache[ck] = (time.time(), res)
    return res


@bp.route('/api/commodity/<key>')
def api_commodity(key):
    tf = request.args.get('tf', '4h')
    if key not in SYMBOLS or tf not in TFS:
        return jsonify(error='商品或週期不正確'), 400
    try:
        res = analyze(key, tf)
    except Exception as e:
        return jsonify(error=f'資料下載失敗：{e}'), 503
    if not res:
        return jsonify(error='資料不足'), 503
    return jsonify(res)


# ── 進出場通知 ────────────────────────────────────────────────────────
# 帳號設定 ALERT_KEY = ['gold:4h', ...]；ALERT_SENT_KEY 記每個組合最後通知過的進場／出場時間。
# 開啟時先記下目前最新的一筆當基準，之後出現更新的進場或出場才送，不補送舊訊號。
ALERT_KEY = 'commodity_alerts'
ALERT_SENT_KEY = 'commodity_alerts_sent'
ALERT_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.commodity_push.lock')
SITE_URL = os.environ.get('NOTIFY_SITE_URL', notify.VAPID_SUB)


def _combo_ok(c):
    k, _, tf = c.partition(':')
    return k in SYMBOLS and tf in TFS


def alert_items(user):
    return [c for c in (userdata.get(user, ALERT_KEY) or []) if _combo_ok(c)]


def _marks(res):
    """(最新一筆進場時間, 最新一筆已出場的出場時間)。"""
    tr = res['trades']
    entry = tr[-1]['entry_time'] if tr else None
    closed = [t for t in tr if not t['open']]
    return entry, (closed[-1]['exit_time'] if closed else None)


def _fmt(v):
    return f'{v:,.2f}'


def alert_texts(res, sent):
    """比對上次通知的位置，回傳要送的訊息（可能是出場、進場或兩者）。"""
    head = f"【StockLens {res['name']} {res['tf_name']}】"
    link = f"{SITE_URL}/commodity?sym={res['key']}&tf={res['tf']}"
    out = []
    tr = res['trades']
    closed = [t for t in tr if not t['open']]
    if closed and closed[-1]['exit_time'] != sent.get('exit') and closed[-1]['entry_time'] == sent.get('entry'):
        t = closed[-1]       # 上次通知過進場的那筆出場了
        out.append(f"{head}出場\n出場 {_fmt(t['exit'])}（{t['exit_label']}，觸及出場線）\n"
                   f"進場 {_fmt(t['entry'])} → 報酬 {t['ret']:+.2f}%（{t['R']:+.2f}R），持有 {t['bars']} 根\n{link}")
    if tr and tr[-1]['entry_time'] != sent.get('entry'):
        t = tr[-1]
        if t['open']:
            stop = res['position']['stop']
            out.append(f"{head}進場訊號\n進場 {_fmt(t['entry'])}（{t['entry_label']}）\n"
                       f"出場線（停損）{_fmt(stop)}，距離 {(stop / t['entry'] - 1) * 100:+.1f}%\n"
                       f"參考目標 2R {_fmt(t['target'])}\n趨勢回檔進場，跌破出場線就出場。僅供參考。\n{link}")
    return out


def run_alerts():
    results = {}
    for user, items in userdata.users_with(ALERT_KEY):
        combos = [c for c in (items or []) if _combo_ok(c)]
        if not combos:
            continue
        sent_all = userdata.get(user, ALERT_SENT_KEY) or {}
        for c in combos:
            if c not in results:
                k, _, tf = c.partition(':')
                try:
                    results[c] = analyze(k, tf)
                except Exception as e:
                    print(f'[commodity] {c}: {e}')
                    results[c] = None
            res = results[c]
            if not res:
                continue
            sent = sent_all.get(c)
            entry, exit_ = _marks(res)
            if sent is None:                              # 剛開啟：只記基準
                sent_all[c] = {'entry': entry, 'exit': exit_}
                continue
            msgs = alert_texts(res, sent)
            sent_all[c] = {'entry': entry, 'exit': exit_}
            userdata.put(user, ALERT_SENT_KEY, sent_all)     # 先記再送，失敗不重送
            if msgs and notify.ready(user):
                ok, err = notify.send(user, '\n\n'.join(msgs), url=f'/commodity?sym={res["key"]}&tf={res["tf"]}')
                if not ok:
                    print(f'[commodity] notify {user} {c}: {err}')
        userdata.put(user, ALERT_SENT_KEY, sent_all)


def _alert_loop():
    lock = open(ALERT_LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    time.sleep(120)
    while True:
        try:
            run_alerts()
        except Exception as e:
            print(f'[commodity] alerts: {e}')
        time.sleep(600)


@bp.route('/api/commodity/alerts', methods=['GET', 'POST'])
def api_alerts():
    user = userdata.current()
    if request.method == 'POST':
        b = request.get_json(silent=True) or {}
        combo, on = b.get('combo', ''), bool(b.get('on'))
        if not _combo_ok(combo):
            return jsonify(error='商品或週期不正確'), 400
        items = [c for c in alert_items(user) if c != combo] + ([combo] if on else [])
        userdata.put(user, ALERT_KEY, items)
        sent = userdata.get(user, ALERT_SENT_KEY) or {}
        sent.pop(combo, None)
        if on:
            k, _, tf = combo.partition(':')
            try:
                entry, exit_ = _marks(analyze(k, tf))
                sent[combo] = {'entry': entry, 'exit': exit_}
            except Exception:
                pass                                      # 背景第一次跑到時再記基準
        userdata.put(user, ALERT_SENT_KEY, sent)
    return jsonify(items=alert_items(user), ready=notify.ready(user))


def init(app, login_required):
    @app.route('/commodity')
    @login_required
    def commodity_page():
        return render_template('commodity.html')

    app.register_blueprint(bp)
    if not os.environ.get('TW_BOARD_NO_BG'):
        threading.Thread(target=_alert_loop, daemon=True).start()
