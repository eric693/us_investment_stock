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


HTML = """<script>loadChart({"SumMoney":"[[1794009600000,100.5],[1794096000000,-20.25],[1794182400000,30.0],[1794268800000,40.0]]","Price":"[]"});</script>
<li onclick='goUrl("2330");' title="2026-11-06 八大公股行庫合計買超 Top.1 : 台積電 (2330) 合計：689,498萬元"><span class="w20"></span><span class="w100 name">台積電</span><span class="w70">78,305</span><span class="w70">25,470</span><span class="w70">37,995</span><span class="w70">132,127</span><span class="w70">18,367</span><span class="w70">132,343</span><span class="w70">172,596</span><span class="w70">92,295</span><span class="w70">689,498</span></li>
<li onclick='goUrl("2408");' title="2026-11-06 八大公股行庫合計賣超 Top.1 : 南亞科 (2408) 合計：-35,147萬元"><span class="w70">-1,000</span><span class="w70">0</span><span class="w70">0</span><span class="w70">0</span><span class="w70">0</span><span class="w70">0</span><span class="w70">0</span><span class="w70">-34,147</span><span class="w70">-35,147</span></li>"""


def test_banks_parse_summary_and_fallback(client, ns, tmp_path, monkeypatch):
    nsf, _ = ns
    monkeypatch.setattr(nsf, 'BANKS_FILE', str(tmp_path / 'b' / 'banks.json'))
    r = nsf._parse_banks(HTML)
    assert list(r['series'].values()) == [100.5, -20.25, 30.0, 40.0] and r['list_date'] == '2026-11-06'
    assert r['buy'][0]['code'] == '2330' and r['buy'][0]['total'] == 68.95 and r['buy'][0]['by_bank']['合庫'] == 7.83 and r['buy'][0]['by_bank']['華南永昌'] == 9.23
    assert r['sell'][0]['total'] == -3.51 and r['sell'][0]['by_bank']['華南永昌'] == -3.41
    with pytest.raises(ValueError):
        nsf._parse_banks('<html>改版了</html>')

    calls = []

    def get(url, **k):
        calls.append(url)
        return type('R', (), {'content': HTML.encode(), 'raise_for_status': lambda s: None})()
    monkeypatch.setattr(nsf.requests, 'get', get)
    s = nsf.banks_summary()
    assert s['total'] == 40.0 and s['streak'] == 2 and s['sum5'] == 150.2 and s['stale'] is False
    nsf.banks_summary()
    assert len(calls) == 1                                # 30 分鐘內不重抓

    def boom(url, **k):
        raise OSError('down')
    monkeypatch.setattr(nsf.requests, 'get', boom)
    monkeypatch.setattr(nsf, 'BANKS_TTL', -1)
    s = nsf.banks_summary()
    assert s['stale'] is True and s['total'] == 40.0       # 來源掛掉用舊資料
    login(client, 'amy', 'user12345')
    assert client.get('/api/nsf/banks').get_json()['stale'] is True
