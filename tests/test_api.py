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
    assert len(r.json['rows']) == 20
    r = client.post('/api/tw/board/screen/after', json={'rules': [{'investor': 'trust', 'side': 'buy'}]})
    assert r.status_code == 200 and 'results' in r.json
    r = client.post('/api/tw/board/backtest', json={'rules': [{'investor': 'trust', 'side': 'buy'}], 'ma_list': [3, 5]})
    assert r.status_code == 200 and [x['ma_n'] for x in r.json['results']] == [3, 5]
