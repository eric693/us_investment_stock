"""帳號之間的個人資料互相看不到。"""
import json

from conftest import login


def two_clients(client):
    import app as app_module
    other = app_module.app.test_client()
    login(client, 'amy', 'user12345')
    login(other, 'boss', 'admin1234')
    return client, other


def test_watchlist_is_per_user(client):
    amy, boss = two_clients(client)
    amy.post('/api/tw/board/watchlist', json={'code': '2330'})
    amy.post('/api/tw/board/watchlist/groups', json={'action': 'add', 'name': 'Amy 的群組'})
    assert boss.get('/api/tw/board/watchlist').json['groups'] == [{'name': '自選股', 'codes': []}]
    assert amy.get('/api/tw/board/watchlist').json['groups'][0]['codes'] == ['2330']


def test_alerts_and_line_are_per_user(client):
    amy, boss = two_clients(client)
    amy.post('/api/tw/line/config', json={'line_token': 'amy-token', 'line_user_id': 'Uamy'})
    amy.post('/api/tw/board/alerts', json={'enabled': True, 'scope': 'all'})
    assert boss.get('/api/tw/line/config').json == {'line_token': '', 'line_user_id': ''}
    assert boss.get('/api/tw/board/alerts').json['enabled'] is False
    assert amy.get('/api/tw/line/config').json['line_token'] == 'amy-token'
    assert amy.get('/api/tw/board/alerts').json['line_ready'] is True


def test_monitor_list_is_per_user(client):
    amy, boss = two_clients(client)
    amy.post('/api/tw/monitor/register', json={'ticker': '2330', 'profile': 'steady'})
    boss.post('/api/tw/monitor/register', json={'ticker': '2317'})
    assert list(amy.get('/api/tw/monitor/list').json) == ['2330.TW']
    assert list(boss.get('/api/tw/monitor/list').json) == ['2317.TW']
    assert 'line_token' not in amy.get('/api/tw/monitor/list').json['2330.TW']
    boss.post('/api/tw/monitor/unregister', json={'ticker': '2330'})      # 刪不到別人的
    assert list(amy.get('/api/tw/monitor/list').json) == ['2330.TW']


def test_strategies_are_per_user(client):
    amy, boss = two_clients(client)
    amy.post('/api/screener/strategies', json={'name': 'amy-strategy', 'conditions': []})
    assert boss.get('/api/screener/strategies').json == {}
    assert list(amy.get('/api/screener/strategies').json) == ['amy-strategy']
    boss.delete('/api/screener/strategies/amy-strategy')
    assert list(amy.get('/api/screener/strategies').json) == ['amy-strategy']


def test_browser_data_is_per_user(client):
    amy, boss = two_clients(client)
    portfolio = json.dumps({'groups': {'g': ['2330']}, 'holdings': [{'t': '2330', 'note': 'AMY-PRIVATE-NOTE'}]})
    assert amy.put('/api/user/kv/pflio_v2', json={'value': portfolio}).status_code == 200
    assert boss.get('/api/user/kv').json == {}
    assert amy.get('/api/user/kv').json == {'pflio_v2': portfolio}
    # 頁面只注入自己帳號的資料
    assert 'AMY-PRIVATE-NOTE' in amy.get('/portfolio').get_data(as_text=True)
    assert 'AMY-PRIVATE-NOTE' not in boss.get('/portfolio').get_data(as_text=True)
    assert amy.put('/api/user/kv/users', json={'value': 'x'}).status_code == 400   # 只能存允許的項目


def test_deleting_user_removes_their_data(client):
    import userdata
    amy, boss = two_clients(client)
    amy.put('/api/user/kv/wl', json={'value': '["AAPL"]'})
    assert boss.post('/api/admin/users', json={'action': 'delete', 'username': 'amy'}).status_code == 200
    assert userdata.get('amy', 'wl') is None


def test_migration_moves_legacy_files(tmp_path, monkeypatch):
    """改版前的共用檔案搬給原帳號，且只在暫存目錄裡動作。"""
    import json, userdata
    monkeypatch.setattr(userdata, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(userdata, 'DB_PATH', str(tmp_path / 'u.db'))
    (tmp_path / 'tw_watchlist.json').write_text(json.dumps(['2330']))
    (tmp_path / 'monitor_config.json').write_text(json.dumps({'line_token': 't', 'line_user_id': 'U1', 'tickers': {}}))
    userdata.migrate_shared_files('boss')
    assert userdata.get('boss', 'tw_watchlist') == {'groups': [{'name': '自選股', 'codes': ['2330']}]}
    assert userdata.get('boss', 'line') == {'token': 't', 'user_id': 'U1'}
    assert (tmp_path / 'tw_watchlist.json.migrated').exists()
    userdata.migrate_shared_files('amy')                 # 只做一次
    assert userdata.get('amy', 'tw_watchlist') is None
