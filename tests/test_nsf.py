"""國安基金：進場後報酬計算、權限、記錄與推播。"""
import numpy as np
import pandas as pd
import pytest

from conftest import login


@pytest.fixture
def ns(board, monkeypatch):
    import fear
    import nsf
    idx = pd.bdate_range('2019-01-01', '2026-12-31')
    px = pd.Series(100 * 1.001 ** np.arange(len(idx)), index=idx)
    monkeypatch.setattr(fear, '_hist', lambda s: px)
    monkeypatch.setattr(nsf, 'ROUNDS', [
        {'n': 1, 'days': 10, 'entry': '2020-03-20', 'exit': '2020-04-20', 'reason': 'a', 'amount': 7.5, 'pnl': 2.5, 'note': ''},
        {'n': 2, 'entry': '2026-06-01', 'exit': None, 'decided': '2026-05-29', 'reason': 'b', 'amount': None, 'pnl': None, 'note': ''}])
    return nsf, px


def test_returns(ns):
    nsf, px = ns
    r = nsf.analyze()
    a = r['rounds'][0]
    k = px.index.searchsorted(pd.Timestamp('2020-03-20'))
    assert a['entry_close'] == pytest.approx(px.iloc[k], abs=0.01) and a['prev_close'] == pytest.approx(px.iloc[k - 1], abs=0.01)
    assert a['r21'] == pytest.approx((1.001 ** 21 - 1) * 100, abs=0.05) and a['status'] == 'closed' and a['days'] == 10
    assert r['open']['n'] == 2 and r['open']['status'] == 'open'
    assert r['stats'][0]['n'] == 2 and r['lows']['n'] == 1


def test_record_admin_and_broadcast(client, ns, monkeypatch):
    nsf, _ = ns
    import app as app_module
    import notify
    sent = []
    monkeypatch.setattr(notify, 'ready', lambda u: True)
    monkeypatch.setattr(notify, 'send', lambda u, t, url='', low=False, line=True: sent.append((u, t, low)) or (True, ''))
    login(client, 'amy', 'user12345')
    assert client.post('/api/nsf/alert', json={'on': True}).get_json()['alert'] is True
    assert client.post('/api/nsf/record', json={'n': 2, 'exit': '2026-08-01'}).status_code == 403
    boss = app_module.app.test_client()
    login(boss, 'boss', 'admin1234')
    assert boss.post('/api/nsf/record', json={'n': 3}).status_code == 400
    assert boss.post('/api/nsf/record', json={'n': 2, 'exit': 'x'}).status_code == 400
    assert boss.post('/api/nsf/record', json={'n': 2, 'amount': '100'}).status_code == 200
    assert sent == []                                      # 只補金額不通知
    assert boss.post('/api/nsf/record', json={'n': 2, 'exit': '2026-08-03'}).status_code == 200
    assert len(sent) == 1 and sent[0][0] == 'amy' and '退場' in sent[0][1] and sent[0][2] is False
    assert boss.post('/api/nsf/record', json={'n': 3, 'entry': '2026-09-01', 'reason': '新'}).status_code == 200
    assert len(sent) == 2 and '第 3 次進場' in sent[1][1]
    j = client.get('/api/nsf').get_json()
    assert j['admin'] is False and j['open']['n'] == 3 and j['rounds'][1]['amount'] == 100 and client.get('/nsf').status_code == 200
