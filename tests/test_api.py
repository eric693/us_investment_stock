"""登入保護、帳號管理與看盤 API。"""
from conftest import login, make_market


def test_everything_requires_login(client):
    assert client.get('/api/tw/board/list').status_code == 401
    r = client.get('/tw/board')
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_login_and_wrong_password(client):
    assert login(client, 'amy', 'nope').status_code == 200          # 留在登入頁
    assert client.get('/api/tw/board/status').status_code == 401
    assert login(client, 'amy', 'user12345').status_code == 302
    assert client.get('/api/tw/board/status').status_code == 200


def test_password_change_logs_out_other_sessions(client, users):
    import app as app_module
    other = app_module.app.test_client()
    login(client, 'amy', 'user12345')
    login(other, 'amy', 'user12345')
    r = client.post('/account', data={'current': 'user12345', 'new': 'newpass99', 'new2': 'newpass99'})
    assert '密碼已更新' in r.get_data(as_text=True)
    assert client.get('/api/tw/board/status').status_code == 200    # 本裝置維持登入
    assert other.get('/api/tw/board/status').status_code == 401     # 其他裝置失效


def test_admin_api_requires_admin(client):
    login(client, 'amy', 'user12345')
    assert client.get('/api/admin/users').status_code == 403
    client.get('/logout')
    login(client, 'boss', 'admin1234')
    r = client.post('/api/admin/users', json={'action': 'add', 'username': 'bob', 'password': 'bobpass12'})
    assert r.status_code == 200 and 'bob' in [u['username'] for u in r.json]
    assert client.post('/api/admin/users', json={'action': 'add', 'username': 'x', 'password': '123'}).status_code == 400
    assert client.post('/api/admin/users', json={'action': 'delete', 'username': 'boss'}).status_code == 400
    assert client.post('/api/admin/users', json={'action': 'role', 'username': 'boss', 'role': 'user'}).status_code == 400


def test_watchlist_groups_api(client):
    login(client, 'amy', 'user12345')
    r = client.post('/api/tw/board/watchlist/groups', json={'action': 'add', 'name': '持有中'})
    assert [g['name'] for g in r.json['groups']] == ['自選股', '持有中']
    r = client.post('/api/tw/board/watchlist', json={'code': '2330', 'group': '持有中'})
    assert r.json['groups'][1]['codes'] == ['2330']
    r = client.delete('/api/tw/board/watchlist', json={'code': '2330'})
    assert r.json['groups'][1]['codes'] == []
    assert client.post('/api/tw/board/watchlist', json={'code': 'bad!'}).status_code == 400


def test_list_and_screen_endpoints(client, board):
    make_market(board, n_days=70, n_codes=20)
    login(client, 'amy', 'user12345')
    r = client.get('/api/tw/board/list', headers={'Accept-Encoding': 'gzip'})
    assert r.status_code == 200 and r.headers.get('Content-Encoding') == 'gzip'
    r = client.get('/api/tw/board/list')
    rows = r.json['rows']
    assert len(rows) == 22 and [x[0] for x in rows[:2]] == ['TAIEX', 'TPEX']   # 大盤指數在最前面
    r = client.post('/api/tw/board/screen/after', json={'rules': [{'investor': 'trust', 'side': 'buy'}]})
    assert r.status_code == 200 and 'results' in r.json
    r = client.post('/api/tw/board/backtest', json={'rules': [{'investor': 'trust', 'side': 'buy'}], 'ma_list': [3, 5]})
    assert r.status_code == 200 and [x['ma_n'] for x in r.json['results']] == [3, 5]


def test_index_chart_and_quote(client, board):
    import sqlite3
    make_market(board, n_days=40, n_codes=5)
    con = sqlite3.connect(board.DB_PATH)
    days = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily ORDER BY date")]
    for i, d in enumerate(days):
        con.execute('INSERT INTO index_daily VALUES(?,?,?,?,?,?)', ('TAIEX', d, 100 + i, 102 + i, 99 + i, 101 + i))
        con.execute('INSERT INTO market_inst VALUES(?,?,?,?,?,?)', ('TSE', d, 1.5, -0.5, 0.2, 1.2))
    con.commit()
    login(client, 'amy', 'user12345')
    q = client.get('/api/tw/board/quote/TAIEX').json
    assert q['price'] == 101 + len(days) - 1 and q['chg'] == 1 and q['kind'] == 'index'
    j = client.get('/api/tw/board/chart/TAIEX?period=D').json
    assert len(j['bars']) == len(days) and j['bars'][0]['volume'] > 0          # 成交金額由個股加總
    assert j['inst']['hold']['foreign'][-1] == round(1.5 * len(days), 2)       # 累計買賣超（億）
    w = client.get('/api/tw/board/chart/TAIEX?period=W').json
    assert len(w['bars']) < len(days) and w['bars'][-1]['close'] == 101 + len(days) - 1
    assert client.get('/api/tw/board/chart/TPEX?period=60').status_code == 400   # 櫃買沒有盤中歷史
    idx = client.get('/api/tw/board/indices').json
    assert [x['code'] for x in idx] == ['TAIEX', 'TPEX'] and len(idx[0]['spark']) == min(60, len(days))


def test_emerging_quote_and_chart(client, board, monkeypatch):
    # 興櫃不在資料庫：報價取自興櫃行情表，K 線走 yfinance 的 .TWO
    esb = {'7719': dict(name='碳基', prev=43.13, high=43.35, low=42.45, avg=42.75, price=42.85,
                        volume=77, date='2026-10-20')}
    monkeypatch.setattr(board, '_esb_quotes', lambda: esb)
    seen = []
    monkeypatch.setattr(board, '_yf_bars_symbol', lambda sym, period: seen.append(sym) or
                        [dict(time='2026-10-20', day='2026-10-20', open=43, high=43.35, low=42.45, close=42.85, volume=77)])
    login(client, 'amy', 'user12345')
    q = client.get('/api/tw/board/quote/7719').json
    assert q['market'] == 'ESB' and q['name'] == '碳基' and q['chg'] == -0.28 and q['chg_pct'] == -0.65
    j = client.get('/api/tw/board/chart/7719?period=D').json
    assert j['market'] == 'ESB' and len(j['bars']) == 1 and seen == ['7719.TWO'] and j['inst']['days'] == []
    assert board.stock_name('7719') == '碳基'
    assert client.get('/api/tw/board/quote/9999').status_code == 404


def test_market_closed_on_typhoon_day(board, monkeypatch):
    """颱風假：開盤時段內證交所回報的成交日期仍是前一天，就不算盤中。"""
    import pandas as pd
    now = pd.Timestamp('2026-10-23 11:16', tz='Asia/Taipei')      # 週五
    monkeypatch.setattr(board, '_now', lambda: now)
    monkeypatch.setattr(board, '_read_rt', lambda: {'ts': '2026-10-23 11:16:55', 'date': '20261022'})
    assert board._market_open(now) is True                  # 指定時間只看時鐘
    assert board._market_open() is False
    monkeypatch.setattr(board, '_read_rt', lambda: {'ts': '2026-10-23 11:16:55', 'date': '20261023'})
    assert board._market_open() is True
    monkeypatch.setattr(board, '_read_rt', lambda: {'ts': '2026-10-23 09:02:00', 'date': '20261022'})
    assert board._market_open() is True                     # 9:10 前還沒成交，先不判定休市
