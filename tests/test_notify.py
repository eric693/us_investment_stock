"""通知：LINE 金鑰驗證、錯誤原因、Web Push 訂閱、同一輪訊號合併成一則。"""
import json
import types

import pytest

import notify
import userdata
from conftest import login

GOOD_UID = 'U' + 'a' * 32


class Resp:
    def __init__(self, code, text=''):
        self.status_code, self.text, self.ok = code, text, code == 200


@pytest.fixture
def nclient(client, tmp_path, monkeypatch):
    monkeypatch.setattr(notify, 'VAPID_FILE', str(tmp_path / 'vapid.json'))
    notify._vapid_cache.clear()
    login(client, 'amy', 'user12345')
    return client


def test_line_save_rejects_bad_user_id(nclient, monkeypatch):
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: Resp(401))
    r = nclient.post('/api/tw/line/config', json={'line_token': 'short', 'line_user_id': '0912345678'})
    err = r.get_json()['error']
    assert r.status_code == 400 and 'User ID' in err and 'token' in err     # 兩個問題一次講完
    assert userdata.get('amy', 'line') is None


def test_line_save_rejects_invalid_token(nclient, monkeypatch):
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: Resp(401))
    r = nclient.post('/api/tw/line/config', json={'line_token': 'short', 'line_user_id': GOOD_UID})
    assert r.status_code == 400 and 'token' in r.get_json()['error']
    assert userdata.get('amy', 'line') is None


def test_line_save_ok_and_clear(nclient, monkeypatch):
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: Resp(200))
    assert nclient.post('/api/tw/line/config', json={'line_token': 'tok', 'line_user_id': GOOD_UID}).status_code == 200
    assert notify.channels('amy')['line']
    assert nclient.post('/api/tw/line/config', json={'line_token': '', 'line_user_id': ''}).status_code == 200
    assert not notify.channels('amy')['line']


def test_quota_error_is_reported(nclient, monkeypatch):
    userdata.put('amy', 'line', {'token': 'tok', 'user_id': GOOD_UID})
    monkeypatch.setattr(notify.requests, 'post', lambda *a, **k: Resp(429, '{"message":"quota"}'))
    ok, err = notify.send('amy', '標題\n內容')
    assert not ok and '額度' in err
    st = nclient.get('/api/notify/status').get_json()
    assert st['line']['ok'] is False and any('額度' in e for e in st['errors'])
    assert st['vapid_public']


def test_web_push_subscribe_send_and_drop_expired(nclient, monkeypatch):
    sub = {'endpoint': 'https://push.example/abc', 'keys': {'p256dh': 'k', 'auth': 'a'}}
    assert nclient.post('/api/notify/subscribe', json=sub).get_json()['devices'] == 1
    assert nclient.post('/api/notify/subscribe', json=sub).get_json()['devices'] == 1   # 同一台不重複
    assert notify.channels('boss')['web'] == 0                                         # 帳號之間互不相通

    sent = []
    fake = types.ModuleType('pywebpush')

    class WebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    def webpush(info, data, **kw):
        sent.append((info['endpoint'], json.loads(data)))
        if info['endpoint'].endswith('gone'):
            raise WebPushException('gone', response=Resp(410))

    fake.webpush, fake.WebPushException = webpush, WebPushException
    monkeypatch.setitem(__import__('sys').modules, 'pywebpush', fake)

    nclient.post('/api/notify/subscribe', json=dict(sub, endpoint='https://push.example/gone'))
    r = nclient.post('/api/notify/test')
    assert r.status_code == 200
    assert {e for e, _ in sent} == {'https://push.example/abc', 'https://push.example/gone'}
    assert sent[0][1]['title'].startswith('【StockLens】')
    assert [s['endpoint'] for s in notify.web_subs('amy')] == ['https://push.example/abc']

    nclient.post('/api/notify/unsubscribe', json={'endpoint': 'https://push.example/abc'})
    assert notify.web_subs('amy') == []


def test_test_without_channels(nclient):
    r = nclient.post('/api/notify/test')
    assert r.status_code == 400


def test_monitor_outbox_merges_per_user(client, monkeypatch):
    import app as app_module
    sent = []
    monkeypatch.setattr(notify, 'send', lambda user, text, url='': sent.append((user, text)) or (True, ''))
    app_module._flush_outbox({'amy': ['訊號一', '訊號二', '訊號三'], 'boss': ['只有一則']})
    assert len(sent) == 2
    amy = dict(sent)['amy']
    assert '共 3 則' in amy and '訊號一' in amy and '訊號三' in amy
    assert dict(sent)['boss'] == '只有一則'


def test_push_failure_lists_every_problem(nclient, monkeypatch):
    userdata.put('amy', 'line', {'token': 'short', 'user_id': '0912345678'})
    monkeypatch.setattr(notify.requests, 'post', lambda *a, **k: Resp(401))
    ok, err = notify.push_line('amy', 'hi')
    assert not ok and 'User ID' in err and 'token' in err
