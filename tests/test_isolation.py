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


def test_alerts_and_line_are_per_user(client, monkeypatch):
    import notify
    monkeypatch.setattr(notify, 'check_line', lambda token, uid: '')    # 不真的打 LINE 驗證
    amy, boss = two_clients(client)
    amy.post('/api/tw/line/config', json={'line_token': 'amy-token', 'line_user_id': 'Uamy'})
    amy.post('/api/tw/board/alerts', json={'enabled': True, 'scope': 'all'})
    assert boss.get('/api/tw/line/config').json == {'has_token': False, 'line_user_id': '', 'has_secret': False}
    assert boss.get('/api/tw/board/alerts').json['enabled'] is False
    cfg = amy.get('/api/tw/line/config').json
    assert cfg['has_token'] and 'line_token' not in cfg          # token 不回傳給瀏覽器
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


def test_newer_settings_are_per_user(client, monkeypatch):
    """每日潛力股、黃金原油通知、恐慌指標提醒、LINE 額度與綁定碼、手機推播訂閱，都只屬於自己的帳號。"""
    import commodity
    import notify
    import userdata
    monkeypatch.setattr(commodity, 'analyze', lambda k, tf: None)
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: type('R', (), {'ok': True, 'status_code': 200, 'json': lambda s: {}})())
    amy, boss = two_clients(client)
    amy.post('/api/picks/daily', json={'tw': True, 'n': 10, 'time': {'tw': '21:00'}})
    amy.post('/api/commodity/alerts', json={'combo': 'gold:4h', 'on': True})
    amy.post('/api/fear/alerts', json={'thresholds': [40], 'term': True})
    amy.post('/api/tw/line/config', json={'line_token': 'amy-tok', 'line_user_id': '', 'line_secret': '0' * 32})
    amy.post('/api/notify/line/cap', json={'cap': 50})
    amy.post('/api/notify/line/bind')
    amy.post('/api/notify/subscribe', json={'endpoint': 'https://push.example/amy', 'keys': {'p256dh': 'x', 'auth': 'y'}})

    b = boss.get('/api/picks/daily').get_json()
    assert not b['tw'] and b['n'] == 5 and b['time']['tw'] == '20:30'
    assert boss.get('/api/commodity/alerts').get_json()['items'] == []
    f = boss.get('/api/fear/alerts').get_json()
    assert f['thresholds'] == [] and not f['term']
    assert boss.get('/api/tw/line/config').get_json() == {'has_token': False, 'line_user_id': '', 'has_secret': False}
    assert boss.get('/api/notify/status').get_json()['web']['devices'] == 0
    assert userdata.get('boss', notify.BIND_KEY) is None and userdata.get('boss', 'line') is None

    a = amy.get('/api/picks/daily').get_json()
    assert a['tw'] and a['n'] == 10 and amy.get('/api/commodity/alerts').get_json()['items'] == ['gold:4h']
    assert userdata.get('amy', 'line')['cap'] == 50 and userdata.get('amy', notify.BIND_KEY)['code']
