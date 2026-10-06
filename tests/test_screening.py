"""選股計算：與逐筆、不向量化的參考實作比對。"""
import json

import numpy as np
import pandas as pd

from conftest import make_market


def ref_kd(high, low, close, n=9):
    k = d = 50.0
    ks, ds = [], []
    for i in range(len(close)):
        s = max(0, i - n + 1)
        hh, ll = max(high[s:i + 1]), min(low[s:i + 1])
        rsv = 50.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds


def ref_ma(a, n):
    return [None if i < n - 1 else sum(a[i - n + 1:i + 1]) / n for i in range(len(a))]


def up(a, b, i):
    return None not in (a[i - 1], b[i - 1], a[i], b[i]) and a[i - 1] <= b[i - 1] and a[i] > b[i]


def dn(a, b, i):
    return None not in (a[i - 1], b[i - 1], a[i], b[i]) and a[i - 1] >= b[i - 1] and a[i] < b[i]


def series(board, code, col):
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    rows = con.execute(f'SELECT {col} FROM daily WHERE code=? ORDER BY date', (code,)).fetchall()
    return [r[0] for r in rows]


def inst(board, code, col):
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    return [r[0] for r in con.execute(f'SELECT {col} FROM inst WHERE code=? ORDER BY date', (code,))]


def test_kd_matches_reference(board):
    rng = np.random.default_rng(1)
    c = pd.DataFrame(rng.normal(100, 5, (60, 3)), columns=list('abc'))
    h, l = c + rng.uniform(0, 2, c.shape), c - rng.uniform(0, 2, c.shape)
    k, d = board.calc_kd_frame(h, l, c)
    for col in c:
        rk, rd = ref_kd(h[col].tolist(), l[col].tolist(), c[col].tolist())
        assert np.allclose(k[col].values, rk) and np.allclose(d[col].values, rd)


def test_cross_detection(board):
    a = pd.DataFrame({'x': [1, 2, 3, 2, 1]})
    b = pd.DataFrame({'x': [2, 2, 2, 2, 2]})
    assert board._cross_up(a, b)['x'].tolist() == [False, False, True, False, False]
    assert board._cross_dn(a, b)['x'].tolist() == [False, False, False, False, True]


def test_after_hours_screen_matches_reference(board):
    dates, codes = make_market(board, n_days=90, n_codes=60)
    for investor, col in [('trust', 'trust_net'), ('foreign', 'foreign_net'), ('total', 'total_net')]:
        for side in ('buy', 'sell'):
            for ma_n in (3, 5):
                res = board.screen_after_hours([{'investor': investor, 'side': side}], ma_n=ma_n)
                got = {r['code'] for r in res['results']}
                want = set()
                for c in codes:
                    hold = list(np.cumsum(inst(board, c, col)))
                    ma = ref_ma(hold, ma_n)
                    i = len(hold) - 1
                    cross = up if side == 'buy' else dn
                    if cross(hold, ma, i):                       # 只看持股線穿越均線（不含 KD）
                        want.add(c)
                assert got == want, (investor, side, ma_n)


def test_backtest_counts_match_reference(board):
    dates, codes = make_market(board, n_days=100, n_codes=30)
    res = board.backtest_after_hours([{'investor': 'trust', 'side': 'buy'}], ma_list=(5,), horizons=(5,))
    warm = 30
    want = 0
    for c in codes:
        hold = list(np.cumsum(inst(board, c, 'trust_net')))
        ma = ref_ma(hold, 5)
        want += sum(1 for i in range(warm, len(hold)) if up(hold, ma, i))
    assert res['results'][0]['signals'] == want
    assert res['results'][0]['stats'][5]['n'] <= want


def test_realtime_intraday_equals_close(board, monkeypatch):
    """盤中把即時報價接在昨天之後，結果要和收盤後的計算一致。"""
    dates, codes = make_market(board, n_days=60, n_codes=60)
    params = {'k_low': 50, 'k_high': 50}          # 放寬門檻，確保有訊號可比
    base = board.screen_realtime(list(board.RT_RULES), params)
    assert base['results']
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    q = {c: dict(price=cl, prev=cl - ch, open=o, high=h, low=l, volume=v)
         for c, o, h, l, cl, ch, v in con.execute(
             'SELECT code, open, high, low, close, chg, volume FROM daily WHERE date=?', (dates[-1],))}
    orig = board._load_frames
    monkeypatch.setattr(board, '_load_frames', lambda n, asof=None: orig(n, dates[-2]))
    monkeypatch.setattr(board, '_rt_today', lambda: dict(ts='x', date='x', quotes=q))
    monkeypatch.setattr(board, '_now', lambda: pd.Timestamp(dates[-1] + ' 12:00', tz='Asia/Taipei'))
    sim = board.screen_realtime(list(board.RT_RULES), params)
    assert sim['source'] == 'realtime'
    assert sorted((r['code'], r['k'], r['d']) for r in base['results']) == \
        sorted((r['code'], r['k'], r['d']) for r in sim['results'])


def test_watchlist_migrates_old_format(board):
    with open(board.WATCH_FILE, 'w') as f:
        json.dump(['2330', '2317'], f)
    data = board._load_watch()
    assert data == {'groups': [{'name': board.DEFAULT_GROUP, 'codes': ['2330', '2317']}]}


def test_realtime_volume_vs_yesterday(board):
    """即時選股：KD 交叉 + K 門檻 + 今日量 > 昨日量（與逐筆參考實作比對）。"""
    dates, codes = make_market(board, n_days=60, n_codes=80, seed=3)
    params = {'k_low': 50, 'k_high': 50}
    res = board.screen_realtime(['kdj_buy', 'kdj_sell'], params)
    got = {(r['code'], s['rule']) for r in res['results'] for s in r['signals']}
    want = set()
    for c in codes:
        k, d = ref_kd(series(board, c, 'high'), series(board, c, 'low'), series(board, c, 'close'))
        v = series(board, c, 'volume')
        i = len(v) - 1
        if v[i] > v[i - 1]:
            if up(k, d, i) and k[i] < 50:
                want.add((c, 'kdj_buy'))
            if dn(k, d, i) and k[i] > 50:
                want.add((c, 'kdj_sell'))
    assert want and got == want


def test_inst_ranking_matches_reference(board):
    """法人排行：期間買賣超加總、排序、連續天數。"""
    dates, codes = make_market(board, n_days=40, n_codes=30, seed=11)
    for side in ('buy', 'sell'):
        res = board.inst_ranking('trust', side, days=5, by='lots', limit=10)
        sums = {c: sum(inst(board, c, 'trust_net')[-5:]) for c in codes}
        sign = 1 if side == 'buy' else -1
        want = sorted([c for c in codes if sums[c] * sign > 0], key=lambda c: -sums[c] * sign)[:10]
        assert [r['code'] for r in res['results']] == want
        for r in res['results']:
            nets = inst(board, r['code'], 'trust_net')
            streak = 0
            for v in reversed(nets):
                if v * sign > 0:
                    streak += 1
                else:
                    break
            assert r['streak'] == streak and r['net'] == round(sums[r['code']])
