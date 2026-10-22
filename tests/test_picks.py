"""潛力股：評分、回測與月營收的時間對齊。"""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from conftest import make_market, login


@pytest.fixture
def pk(board, tmp_path, monkeypatch):
    import picks
    monkeypatch.setattr(picks, 'CACHE_FILE', str(tmp_path / 'picks_{}.json'))
    monkeypatch.setattr(picks, 'US_DB_PATH', str(tmp_path / 'us.db'))
    picks._init_tw_tables()
    picks._init_us_db()
    return picks


def test_parse_revenue_rows(pk):
    raw = ('<table><tr><th>公司代號</th></tr>'
           '<tr><td>2330</td><td>台積電</td><td>511,857,399</td><td>1</td><td>330,980,920</td><td>2</td></tr>'
           '<tr><td>合計</td><td></td><td>9</td><td>9</td><td>9</td></tr></table>')
    assert pk._parse_revenue(raw) == [('2330', 511857399.0, 330980920.0)]


def test_revenue_used_only_after_publication(pk, board):
    # 2026-01 營收在 2026-02-11 之前不能用，回測才不會偷看
    with board._db() as con:
        con.executemany('INSERT INTO revenue VALUES(?,?,?,?)',
                        [('1101', ym, 120, 100) for ym in ('2025-10', '2025-11', '2025-12')] + [('1101', '2026-01', 200, 100)])
    dates = ['2026-02-10', '2026-02-11', '2026-02-12']
    yoy3, extra = pk._revenue_frame(dates, ['1101'])
    assert yoy3['1101'].tolist() == pytest.approx([20.0, (440 / 300 - 1) * 100, (440 / 300 - 1) * 100])
    assert extra['ym']['1101'].tolist() == ['2025-12', '2026-01', '2026-01']


def test_build_tw_scores_and_backtest(pk, board):
    make_market(board, n_days=220, n_codes=40)
    res = pk.build('tw')
    assert res and res['market'] == 'tw' and res['universe'] == 40
    assert {f['id'] for f in res['factors']} == {'foreign', 'breakout', 'trend', 'trust_hot', '_score'}  # 沒有營收資料就不用營收
    scores = [s['score'] for s in res['stocks']]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 100 for s in scores)
    assert [s['rank'] for s in res['stocks']] == list(range(1, len(scores) + 1))
    for h in pk.HORIZONS:
        r = res['horizons'][h]
        assert [d['d'] for d in r['deciles']] == list(range(1, 11))
        assert r['curve'][0][1:] == [0, 0] and len(r['curve']) > 1
    # 綜合分數 = 各因子百分位等權平均（反向因子取 1 - 排名）
    s = res['stocks'][0]
    vals = [v for k, v in s['f'].items() if v is not None]
    assert s['score'] == pytest.approx(np.mean(vals), abs=1.0)


def test_build_us_from_db(pk):
    rng = np.random.default_rng(3)
    dates = pd.bdate_range('2024-01-02', periods=520).strftime('%Y-%m-%d')
    with pk._us_db() as con:
        for i in range(30):
            code = f'T{i:02d}'
            close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(dates))))
            con.execute('INSERT INTO us_stocks VALUES(?,?,?,?,?)', (code, f'Co {i}', 'Tech' if i % 2 else 'Health', 'S&P 500', '2026-01-01'))
            con.executemany('INSERT INTO us_daily VALUES(?,?,?,?,?,?,?)',
                            [(code, d, c, c * 1.01, c * .99, c, 1e6) for d, c in zip(dates, close)])
        con.execute("INSERT INTO us_sync VALUES('daily', '2026-01-01')")
    res = pk.get_picks('us')
    assert res['universe'] == 30 and len(res['stocks']) == 30
    assert {f['id'] for f in res['factors']} == {'mom12', 'trend200', 'pullback', '_score'}
    assert sum(k for _, k in res['top_groups']) == pk.TOP_N
    assert pk.get_picks('us')['built'] == res['built']          # 版本沒變就讀快取


def test_picks_api(client, pk, board):
    make_market(board, n_days=220, n_codes=40)
    assert client.get('/api/picks/tw').status_code == 401
    login(client, 'amy', 'user12345')
    j = client.get('/api/picks/tw').json
    top = j['stocks'][0]['code']
    one = client.get(f'/api/picks/tw/stock/{top}.TW').json
    assert one['rank'] == 1 and one['factors']
    assert client.get('/api/picks/tw/stock/9999').status_code == 404
    assert client.get('/api/picks/xx').status_code == 400
    assert client.get('/api/picks/us').status_code == 503        # 美股資料還沒下載
    assert client.get('/picks').status_code == 200


def test_commodity_backtest_rules():
    """趨勢回檔進場、移動停損只升不降、碰到停損出場。"""
    import commodity as cm
    rng = np.random.default_rng(3)
    n = 900
    close = 100 * np.exp(np.cumsum(0.002 + rng.normal(0, 0.01, n)))      # 帶漂移的上升走勢
    df = pd.DataFrame({'Open': np.r_[close[0], close[:-1]], 'High': close * 1.004, 'Low': close * 0.996, 'Close': close},
                      index=pd.date_range('2024-01-01', periods=n, freq='4h', tz='America/New_York'))
    trades, st = cm.backtest(df, cost=0.0003)
    assert trades
    for t in trades:
        assert t['stop0'] < t['entry'] and t['target'] == pytest.approx(t['entry'] + 2 * t['risk'])
        assert all(b >= a for a, b in zip(t['stops'], t['stops'][1:]))           # 出場線只升不降
        if not t['open']:
            assert df['Low'].iloc[t['exit_i']] <= t['stops'][-1] and t['exit'] <= t['stops'][-1] + 1e-9
    assert all(a['exit_i'] < b['entry_i'] for a, b in zip(trades, trades[1:]))  # 同時只有一筆
    assert st['state'] in ('holding', 'pullback', 'waiting', 'no_trend')


def test_commodity_api(client, monkeypatch):
    import commodity as cm
    rng = np.random.default_rng(5)
    n = 700
    close = 1800 * np.exp(np.cumsum(0.001 + rng.normal(0, 0.008, n)))
    df = pd.DataFrame({'Open': close, 'High': close * 1.003, 'Low': close * 0.997, 'Close': close},
                      index=pd.date_range('2025-01-01', periods=n, freq='4h', tz='America/New_York'))
    monkeypatch.setattr(cm, '_download', lambda ticker, tf: df)
    cm._cache.clear()
    login(client, 'amy', 'user12345')
    assert client.get('/api/commodity/silver?tf=4h').status_code == 400
    r = client.get('/api/commodity/gold?tf=4h').get_json()
    assert r['name'] == '黃金' and len(r['bars']) == n and r['stats']['all']['n'] == len([t for t in r['trades'] if not t['open']])
    assert set(r['stats']) == {'all', 'first', 'second'}
    assert client.get('/commodity').status_code == 200


def _fake_res(trades):
    return {'key': 'gold', 'tf': '4h', 'name': '黃金', 'tf_name': '4 小時', 'trades': trades,
            'position': {'stop': 95.0} if trades and trades[-1]['open'] else None}


def _t(entry_time, open_, exit_time=None):
    return {'entry_time': entry_time, 'exit_time': exit_time or entry_time, 'entry_label': f'E{entry_time}',
            'exit_label': f'X{exit_time}', 'entry': 100.0, 'exit': 103.0, 'target': 110.0, 'ret': 3.0, 'R': 0.6,
            'bars': 5, 'open': open_}


def test_commodity_alerts_entry_then_exit(client, monkeypatch):
    import commodity as cm
    import notify
    import userdata
    sent, state = [], {'trades': [_t(1, False, 2)]}
    monkeypatch.setattr(cm, 'analyze', lambda k, tf: _fake_res(state['trades']))
    monkeypatch.setattr(notify, 'ready', lambda u: True)
    monkeypatch.setattr(notify, 'send', lambda u, text, url='': sent.append(text) or (True, ''))
    login(client, 'amy', 'user12345')
    r = client.post('/api/commodity/alerts', json={'combo': 'gold:4h', 'on': True}).get_json()
    assert r['items'] == ['gold:4h']
    assert client.post('/api/commodity/alerts', json={'combo': 'silver:4h', 'on': True}).status_code == 400

    cm.run_alerts()
    assert sent == []                                   # 開啟時的舊交易不補送
    state['trades'] = [_t(1, False, 2), _t(5, True)]
    cm.run_alerts(); cm.run_alerts()
    assert len(sent) == 1 and '進場訊號' in sent[0] and '出場線（停損）95.00' in sent[0]
    state['trades'] = [_t(1, False, 2), _t(5, False, 9)]
    cm.run_alerts()
    assert len(sent) == 2 and '出場' in sent[1] and '+3.00%' in sent[1]
    client.post('/api/commodity/alerts', json={'combo': 'gold:4h', 'on': False})
    state['trades'].append(_t(12, True))
    cm.run_alerts()
    assert len(sent) == 2 and userdata.get('amy', cm.ALERT_KEY) == []
