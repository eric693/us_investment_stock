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
    import userdata
    userdata.put('amy', 'tw_watchlist', ['2330', '2317'])
    data = board._load_watch('amy')
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


def test_line_alerts_pipeline(board, monkeypatch):
    """盤中推播：符合條件的自選股推一次，同一檔同一條件當天不重複；不在範圍內的不推。"""
    dates, codes = make_market(board, n_days=60, n_codes=80, seed=3)
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    q = {c: dict(price=cl, prev=cl - ch, open=o, high=h, low=l, volume=v)
         for c, o, h, l, cl, ch, v in con.execute(
             'SELECT code, open, high, low, close, chg, volume FROM daily WHERE date=?', (dates[-1],))}
    orig = board._load_frames
    monkeypatch.setattr(board, '_load_frames', lambda n, asof=None: orig(n, dates[-2]))
    monkeypatch.setattr(board, '_rt_today', lambda: dict(ts='t', date='d', quotes=q))
    monkeypatch.setattr(board, '_now', lambda: pd.Timestamp(dates[-1] + ' 10:00', tz='Asia/Taipei'))
    params = {'k_low': 50, 'k_high': 50}
    hits = {r['code'] for r in board.screen_realtime(['kdj_buy', 'kdj_sell'], params)['results']}
    assert len(hits) >= 2
    watched = sorted(hits)[:1] + [c for c in codes if c not in hits][:1]   # 一檔會中、一檔不會
    board._save_watch('amy', {'groups': [{'name': '自選股', 'codes': watched}]})
    sent = []
    monkeypatch.setattr(board, '_push_line', lambda text, user: (sent.append((user, text)), (True, ''))[1])
    board._save_alerts('amy', {'enabled': True, 'scope': 'watch', 'rules': ['kdj_buy', 'kdj_sell'],
                        'params': params, 'include_etf': False, 'sent': {}, 'log': []})
    board._check_alerts()
    assert len(sent) == 1 and sent[0][0] == 'amy' and watched[0] in sent[0][1] and watched[1] not in sent[0][1]
    board._check_alerts()                      # 同一天再跑：不重複推
    assert len(sent) == 1
    log = board._load_alerts('amy')['log']
    assert [x['code'] for x in log] == [watched[0]] * len(log) and all(x['pushed'] for x in log)


def test_realtime_poll_retry_and_total_failure(board, monkeypatch):
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    con.execute("INSERT INTO stocks VALUES('2330','台積電','TSE','半導體','stock','x')")
    con.commit()
    monkeypatch.setattr(board.time, 'sleep', lambda s: None)
    calls = []

    class Resp:
        def __init__(self, ok): self.ok = ok
        def json(self):
            if not self.ok:
                raise ValueError('not json')
            return {'msgArray': [{'c': '2330', 'z': '1000', 'y': '990', 'o': '995', 'h': '1001', 'l': '994', 'v': '5000'}]}

    # 第一次失敗、重試成功
    monkeypatch.setattr(board.requests, 'get', lambda *a, **k: (calls.append(1), Resp(len(calls) > 1))[1])
    snap = board._poll_realtime_once({})
    assert snap['quotes']['2330']['price'] == 1000 and snap['failed_chunks'] == 0 and len(calls) == 2
    # 整輪失敗：回傳上一次的快照（時間戳不更新）
    monkeypatch.setattr(board.requests, 'get', lambda *a, **k: Resp(False))
    assert board._poll_realtime_once(snap) is snap


def _add_issued(board, codes, lots=50000):
    import sqlite3
    con = sqlite3.connect(board.DB_PATH)
    con.executemany('INSERT INTO issued VALUES(?,?,?)', [(c, 'x', lots) for c in codes])
    con.commit()


def test_ratio_screen_matches_reference(board):
    """持股比例變化 = N 日買賣超合計 ÷ 發行張數。"""
    dates, codes = make_market(board, n_days=40, n_codes=40, seed=5)
    _add_issued(board, codes, lots=20000)          # 買賣超約 ±50 張/日 → 5 日約 ±1%
    for investor, col in [('trust', 'trust_net'), ('total', 'total_net')]:
        for side in ('buy', 'sell'):
            for days, th in [(5, 0.5), (10, 1.0)]:
                res = board.screen_ratio([{'investor': investor, 'side': side}], days=days, threshold=th)
                got = {r['code']: r['ratio_chg'] for r in res['results']}
                want = {}
                for c in codes:
                    chg = sum(inst(board, c, col)[-days:]) / 20000 * 100
                    if (chg >= th) if side == 'buy' else (chg <= -th):
                        want[c] = round(chg, 2)
                assert got == want, (investor, side, days, th)
    assert any(board.screen_ratio([{'investor': 'total', 'side': 'buy'}], days=5, threshold=0.5)['results'])


def test_ratio_optimizer_counts_trigger_days(board):
    """最佳化：訊號只算條件剛成立那天，並分訓練 / 測試期。"""
    dates, codes = make_market(board, n_days=120, n_codes=30, seed=9)
    _add_issued(board, codes, lots=20000)
    res = board.optimize_ratio([{'investor': 'trust', 'side': 'buy'}], days_list=(5,), th_list=(0.5,),
                               horizon=5, min_signals=1)
    cell = res['results'][0]['grid'][0]
    n = len(dates)
    split, warm = int(n * 0.7), 6
    want = {'train': 0, 'test': 0}
    for c in codes:
        net = inst(board, c, 'trust_net')
        cond = [i >= 4 and sum(net[i - 4:i + 1]) / 20000 * 100 >= 0.5 for i in range(n)]
        for i in range(warm, n - 5):                      # 最後 5 天沒有之後報酬
            if cond[i] and not cond[i - 1]:
                want['train' if i < split else 'test'] += 1
    assert cell['train']['n'] == want['train'] and (cell['test'] or {'n': 0})['n'] == want['test']
    assert res['results'][0]['best']['days'] == 5


def test_sector_ranking_cap_weighted(board):
    """類股漲跌：市值加權 = Σ(今價×股數)/Σ(前價×股數) − 1；等權 = 個股漲跌平均。"""
    import sqlite3
    dates, codes = make_market(board, n_days=30, n_codes=12, seed=4)
    con = sqlite3.connect(board.DB_PATH)
    for i, c in enumerate(codes):
        con.execute('UPDATE stocks SET industry=? WHERE code=?', ('甲類' if i % 2 else '乙類', c))
        con.execute('INSERT INTO issued VALUES(?,?,?)', (c, 'x', 1000 * (i + 1)))
    con.commit()
    res = board.sector_ranking(5)
    for s in res['sectors']:
        members = [(i, c) for i, c in enumerate(codes) if ('甲類' if i % 2 else '乙類') == s['industry']]
        now = {c: series(board, c, 'close')[-1] for _, c in members}
        base = {c: series(board, c, 'close')[-6] for _, c in members}
        cap = sum(now[c] * 1000 * (i + 1) for i, c in members) / sum(base[c] * 1000 * (i + 1) for i, c in members) - 1
        avg = sum(now[c] / base[c] - 1 for _, c in members) / len(members)
        assert s['cap_ret'] == round(cap * 100, 2) and s['avg_ret'] == round(avg * 100, 2) and s['count'] == len(members)


def test_ma_squeeze_rule_matches_reference(board):
    dates, codes = make_market(board, n_days=60, n_codes=150, seed=21)
    for band in (1.0, 3.0):
        res = board.screen_realtime(['ma_squeeze'], {'ma_band': band})
        got = {r['code'] for r in res['results']}
        want = set()
        for c in codes:
            cl, v = series(board, c, 'close'), series(board, c, 'volume')
            ma = lambda n, i: sum(cl[i - n + 1:i + 1]) / n
            i = len(cl) - 1
            m5, m10, m20 = ma(5, i), ma(10, i), ma(20, i)
            if (ma(20, i) > ma(20, i - 1) and (max(m5, m10, m20) / min(m5, m10, m20) - 1) * 100 <= band
                    and cl[i - 1] <= ma(5, i - 1) and cl[i] > m5 and v[i] > v[i - 1]):
                want.add(c)
        assert got == want, band
    assert board.screen_realtime(['ma_squeeze'], {'ma_band': 3.0})['results']
