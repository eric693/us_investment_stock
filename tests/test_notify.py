"""通知：LINE 金鑰驗證、錯誤原因、Web Push 訂閱、同一輪訊號合併成一則。"""
import json
import types

import pytest

import notify
import userdata
from conftest import login

GOOD_UID = 'U' + 'a' * 32


class Resp:
    def __init__(self, code, text='', data=None):
        self.status_code, self.text, self.ok = code, text, code == 200
        self.data = data if data is not None else {}

    def json(self):
        return self.data


@pytest.fixture(autouse=True)
def _fresh_quota():
    notify._usage_cache.clear()


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


def _fake_picks(end):
    return {'end': end, 'stocks': [
        {'code': str(2330 + i), 'rank': i + 1, 'name': f'股{i}', 'score': 90 - i, 'close': 100.0,
         'chg_pct': 1.5, 'stop': 95.0, 'why': ['外資買超', '站上月線', '營收年增'], 'risk': []}
        for i in range(12)]}


def _at(ts):
    import pandas as pd
    return pd.Timestamp(ts, tz='Asia/Taipei')


@pytest.fixture
def daily(nclient, monkeypatch):
    import picks
    sent, data = [], {}
    monkeypatch.setattr(notify, 'send', lambda user, text, url='': sent.append(text) or (True, ''))
    monkeypatch.setattr(picks, '_cached', lambda m: data.get(m))
    userdata.put('amy', 'line', {'token': 'tok', 'user_id': GOOD_UID})
    return picks, sent, data


def test_daily_picks_trading_days_at_set_time(nclient, daily):
    picks, sent, data = daily
    r = nclient.post('/api/picks/daily', json={'tw': True, 'n': 3, 'time': {'tw': '21:00'}}).get_json()
    assert r['tw'] and not r['us'] and r['n'] == 3 and r['freq'] == 'trading' and r['time']['tw'] == '21:00'

    data['tw'] = _fake_picks('2026-10-21')
    data['us'] = _fake_picks('2026-10-20')
    picks.run_schedule(_at('2026-10-21 20:59'))           # 還沒到時間
    assert sent == []
    picks.run_schedule(_at('2026-10-21 21:00'))
    picks.run_schedule(_at('2026-10-21 21:30'))           # 同一天不重送；沒開美股
    assert len(sent) == 1
    assert sent[0].startswith('【StockLens 台股潛力股】2026-10-21 前 3 名')
    assert '3. 2332 股2' in sent[0] and '4. ' not in sent[0] and '/picks?market=tw' in sent[0]

    picks.run_schedule(_at('2026-10-22 21:00'))           # 隔天資料還沒更新：等
    assert len(sent) == 1
    data['tw'] = _fake_picks('2026-10-22')
    picks.run_schedule(_at('2026-10-22 22:10'))           # 資料到了當天補送
    assert len(sent) == 2 and '2026-10-22' in sent[1]


def test_daily_picks_weekly(nclient, daily):
    picks, sent, data = daily
    data['tw'] = _fake_picks('2026-10-21')
    nclient.post('/api/picks/daily', json={'tw': True, 'freq': 'weekly', 'weekday': 4, 'time': {'tw': '08:00'}})
    picks.run_schedule(_at('2026-10-22 09:00'))           # 週四
    assert sent == []
    picks.run_schedule(_at('2026-10-23 08:00'))           # 週五：送最新名單（即使開啟前就有）
    picks.run_schedule(_at('2026-10-23 12:00'))
    assert len(sent) == 1


def test_daily_picks_enable_does_not_backfill(nclient, daily):
    picks, sent, data = daily
    data['tw'] = _fake_picks('2026-10-21')
    nclient.post('/api/picks/daily', json={'tw': True})
    picks.run_schedule(_at('2026-10-21 23:00'))           # 開啟當下已經有的名單不補送
    assert sent == []
    data['tw'] = _fake_picks('2026-10-22')
    picks.run_schedule(_at('2026-10-22 20:30'))
    assert len(sent) == 1


def test_daily_picks_skips_users_without_channel(nclient, daily):
    picks, sent, data = daily
    userdata.delete('amy', 'line')
    nclient.post('/api/picks/daily', json={'tw': True})
    data['tw'] = _fake_picks('2026-10-22')
    picks.run_schedule(_at('2026-10-22 21:00'))
    assert sent == [] and picks._sent('amy').get('tw', {}).get('day', '') == ''   # 設好管道後還會送


def test_line_monthly_cap(nclient, monkeypatch):
    posts = []
    usage = {'n': 199}

    def get(url, **k):
        if url.endswith('/consumption'):
            return Resp(200, data={'totalUsage': usage['n']})
        if url.endswith('/quota'):
            return Resp(200, data={'type': 'limited', 'value': 200})
        return Resp(200)
    monkeypatch.setattr(notify.requests, 'get', get)
    monkeypatch.setattr(notify.requests, 'post', lambda url, **k: posts.append(url) or Resp(200))
    userdata.put('amy', 'line', {'token': 'tok', 'user_id': GOOD_UID})

    assert notify.send('amy', '標題\n第 200 則') == (True, '')
    assert notify.line_usage('amy')['used'] == 200          # 送出後快取的用量 +1
    ok, err = notify.send('amy', '標題\n第 201 則')
    assert not ok and '上限 200' in err and len(posts) == 1
    st = nclient.get('/api/notify/status').get_json()
    assert st['line_usage'] == {'used': 200, 'cap': 200, 'month': notify._month()}

    # 自己設更低的上限（例如留額度給盯盤）
    usage['n'] = 50
    notify._usage_cache.clear()
    assert nclient.post('/api/notify/line/cap', json={'cap': 40}).get_json()['cap'] == 40
    assert not notify.send('amy', '標題\n內容')[0] and len(posts) == 1


SECRET = '0123456789abcdef0123456789abcdef'


def _signed(body, secret=SECRET):
    import base64, hashlib, hmac
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_line_webhook_binds_user_id(nclient, monkeypatch):
    replies = []
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: Resp(200))
    monkeypatch.setattr(notify.requests, 'post', lambda url, **k: replies.append((url, k['json'])) or Resp(200))
    # User ID 留空、填 secret 也能存
    r = nclient.post('/api/tw/line/config', json={'line_token': 'tok', 'line_user_id': '', 'line_secret': SECRET})
    assert r.status_code == 200 and not notify.channels('amy')['line']
    assert nclient.get('/api/notify/status').get_json()['line']['pending']
    code = nclient.post('/api/notify/line/bind').get_json()['code']

    nclient.post('/logout')
    uid = 'U' + 'b' * 32
    body = json.dumps({'events': [{'type': 'message', 'replyToken': 'rt', 'source': {'userId': uid},
                                   'message': {'type': 'text', 'text': code}}]}).encode()
    bad = nclient.post('/api/line/webhook', data=body, headers={'X-Line-Signature': _signed(body, 'f' * 32)})
    assert bad.status_code == 401 and not notify.channels('amy')['line']
    ok = nclient.post('/api/line/webhook', data=body, headers={'X-Line-Signature': _signed(body)})
    assert ok.status_code == 200
    assert userdata.get('amy', 'line')['user_id'] == uid and notify.channels('amy')['line']
    assert userdata.get('amy', notify.BIND_KEY) is None
    assert replies[-1][0].endswith('/reply') and '已綁定' in replies[-1][1]['messages'][0]['text']

    # 綁定碼只能用一次；LINE 後台 Verify 的空事件回 200
    again = nclient.post('/api/line/webhook', data=body, headers={'X-Line-Signature': _signed(body)})
    assert again.status_code == 200 and '不正確' in replies[-1][1]['messages'][0]['text']
    empty = b'{"events":[]}'
    assert nclient.post('/api/line/webhook', data=empty, headers={'X-Line-Signature': _signed(empty)}).status_code == 200


def test_line_bind_needs_secret(nclient, monkeypatch):
    monkeypatch.setattr(notify.requests, 'get', lambda *a, **k: Resp(200))
    nclient.post('/api/tw/line/config', json={'line_token': 'tok', 'line_user_id': GOOD_UID})
    assert nclient.post('/api/notify/line/bind').status_code == 400
    r = nclient.post('/api/tw/line/config', json={'line_token': 'tok', 'line_user_id': '', 'line_secret': ''})
    assert r.status_code == 400 and 'secret' in r.get_json()['error']
