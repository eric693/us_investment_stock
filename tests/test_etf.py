"""台股 ETF：即時淨值解析、成分股解析、重疊度、合併曝險、填息天數。"""
import sqlite3

import pandas as pd
import pytest

from conftest import login

ALL = {'a1': [{'msgArray': [
    {'a': '0050', 'b': '元大台灣50', 'c': '1,000,000,000', 'd': '5,000,000', 'e': '100.5', 'f': '100.0', 'g': '0.5', 'h': '99', 'i': '20261022', 'j': '13:30:00'},
    {'a': '00878', 'b': '國泰永續高股息(基金之配息來源可能為收益平準金)', 'c': 2e9, 'd': -1e6, 'e': 21.0, 'f': 20.6, 'g': 1.94, 'h': 20.5, 'i': '20261022', 'j': '13:30:00'}]}]}


def _page(rows):
    trs = ''.join(f"<tr><td class='col05'><a href='x'>{n}({c}.TW)</a></td><td class='col06'>{p}</td><td class='col07'>{s:,}</td></tr>" for n, c, p, s in rows)
    return (f'<div id="x_sdate3" align="right">資料日期：2026/10/22</div><table class="datalist" id="ctl00_MainContent_stable3">'
            f'<thead><tr><th>個股名稱</th></tr></thead><tbody>{trs}</tbody></table>').encode('utf-8')


HOLD = {'0050': _page([('台積電', '2330', 60.0, 100), ('聯發科', '2454', 10.0, 10), ('中信金', '2891', 5.0, 50)]),
        '00878': _page([('中信金', '2891', 10.0, 90), ('聯發科', '2454', 6.0, 5), ('廣達', '2382', 8.0, 20)])}


class R:
    def __init__(self, js=None, content=b''):
        self._js, self.content = js, content

    def json(self):
        return self._js


@pytest.fixture
def et(board, tmp_path, monkeypatch):
    import etf_tw
    etf_tw._mem.clear()
    monkeypatch.setattr(etf_tw, 'CACHE_DIR', str(tmp_path / 'etf'))

    def get(url, **k):
        if 'all_etf' in url:
            return R(ALL)
        if 'moneydj' in url:
            return R(content=HOLD[url.split('etfid=')[1].split('.')[0]])
        return R({'data': [], 'tables': [{'data': []}]})
    monkeypatch.setattr(etf_tw.requests, 'get', get)
    # 除息 2026-07-20 每單位 1 元，除息前收 100；之後 3 天漲回 100
    dates = pd.bdate_range('2026-07-13', periods=12)
    closes = [100, 100, 100, 100, 100, 99, 99.3, 99.6, 100.2, 101, 101, 101]
    con = sqlite3.connect(board.DB_PATH)
    for code in ('0050', '00878'):
        con.execute('INSERT INTO stocks VALUES(?,?,?,?,?,?)', (code, code, 'TSE', 'ETF', 'etf', '2026-01-01'))
        for d, c in zip(dates.strftime('%Y-%m-%d'), closes):
            con.execute('INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)', (code, d, c, c, c, c, 0, 1))
    con.commit(); con.close()
    monkeypatch.setattr(etf_tw, '_dividends', lambda code: pd.Series([1.0], index=[pd.Timestamp('2026-07-20')]))
    return etf_tw


def test_quotes_and_holdings(et):
    q = et.all_etf()
    assert q['0050']['units'] == 1e9 and q['0050']['size'] == 1000.0 and q['00878']['name'] == '國泰永續高股息'
    h = et.holdings('0050')
    assert h['date'] == '2026-10-22' and h['rows'][0] == {'name': '台積電', 'code': '2330', 'pct': 60.0, 'shares': 100.0}


def test_compare_overlap_exposure_fill(et):
    r = et.compare(['0050', '00878'], [75, 25])
    assert r['overlap'][0][1] == pytest.approx(5.0 + 6.0)        # 中信金 min(5,10) + 聯發科 min(10,6)
    assert {c['code'] for c in r['common']} == {'2891', '2454'}
    exp = {e['code']: e['pct'] for e in r['exposure']}
    assert exp['2330'] == pytest.approx(45.0) and exp['2891'] == pytest.approx(5 * 0.75 + 10 * 0.25)
    i878 = r['items'][1]
    assert i878['premium_warn'] and not r['items'][0]['premium_warn']
    ev = r['items'][0]['div']['events'][0]
    assert ev['pre_close'] == 100 and ev['fill_days'] == 3 and ev['filled']


def test_api(client, et):
    login(client, 'amy', 'user12345')
    assert client.get('/api/etf/compare?codes=abc').status_code == 400
    j = client.get('/api/etf/compare?codes=0050,00878').get_json()
    assert j['codes'] == ['0050', '00878'] and j['weights'] == [50.0, 50.0]
    assert client.get('/api/etf/list').get_json()['rows']
    assert client.get('/etf').status_code == 200
