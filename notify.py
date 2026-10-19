# -*- coding: utf-8 -*-
"""通知：每個帳號的 LINE 推播與手機／瀏覽器推播（Web Push）。

send(user, text) 送到這個帳號設定好的所有管道，每個管道最近一次的結果記在
userdata 'notify_status'，畫面上的錯誤提醒（token 失效、額度用完…）就是讀這份紀錄。
Web Push 的 VAPID 金鑰第一次用到時自動產生，存在 .vapid.json（不進版控）。
"""
import os
import re
import json
import fcntl
import base64
import datetime as dt

import requests
from flask import Blueprint, jsonify, request

import userdata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAPID_FILE = os.path.join(BASE_DIR, '.vapid.json')
VAPID_SUB = os.environ.get('NOTIFY_VAPID_SUB', 'https://us-stock.crownai.ink')
MAX_SUBS = 10                      # 每個帳號最多記幾台裝置
LINE_UID_RE = re.compile(r'^U[0-9a-f]{32}$')

bp = Blueprint('notify', __name__)


def _now():
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M')


# ── LINE ──────────────────────────────────────────────────────────────
def line_creds(user):
    c = userdata.get(user, 'line') or {}
    return c.get('token', ''), c.get('user_id', '')


def _line_error(r):
    """把 LINE API 的錯誤回應翻成看得懂的原因。"""
    if r.status_code == 401:
        return 'LINE token 無效或已過期，請到 LINE Developers 重新發行 Channel access token'
    if r.status_code == 429:
        return 'LINE 本月訊息額度已用完（或短時間發送太多），請到 LINE 官方帳號後台查看方案'
    if r.status_code == 403:
        return '這個 LINE 頻道沒有推播權限，請確認是 Messaging API 頻道'
    if r.status_code == 400 and "'to'" in r.text:
        return 'LINE User ID 不正確，應為 U 開頭、共 33 字（在 LINE Developers 的 Basic settings 最下方）'
    return f'LINE 回應 {r.status_code}：{r.text[:150]}'


def check_line(token, uid):
    """儲存前檢查 LINE 金鑰；回傳錯誤訊息，沒問題回傳空字串。網路錯誤回傳 None（無法判斷）。"""
    errs = []
    if not LINE_UID_RE.match(uid):
        errs.append('LINE User ID 格式不對，應為 U 開頭、共 33 字（在 LINE Developers 的 Basic settings 最下方）')
    try:
        r = requests.get('https://api.line.me/v2/bot/info', timeout=10,
                         headers={'Authorization': f'Bearer {token}'})
        if not r.ok:
            errs.append(_line_error(r))
    except requests.RequestException:
        if not errs:
            return None
    return '；'.join(errs)


def push_line(user, text):
    """回傳 (是否成功, 錯誤訊息)；沒設定 LINE 回傳 (None, '')。"""
    token, uid = line_creds(user)
    if not token or not uid:
        return None, ''
    try:
        r = requests.post('https://api.line.me/v2/bot/message/push', timeout=10,
                          headers={'Authorization': f'Bearer {token}'},
                          json={'to': uid, 'messages': [{'type': 'text', 'text': text[:4900]}]})
    except requests.RequestException as e:
        return False, f'連不到 LINE：{e}'
    if r.ok:
        return True, ''
    errs = [_line_error(r)]
    if not LINE_UID_RE.match(uid) and 'User ID' not in errs[0]:
        errs.insert(0, 'LINE User ID 格式不對，應為 U 開頭、共 33 字（在 LINE Developers 的 Basic settings 最下方）')
    return False, '；'.join(errs)


# ── Web Push ──────────────────────────────────────────────────────────
_vapid_cache = {}


def _vapid():
    """(Vapid 物件, 給瀏覽器的公鑰)。多個 worker 同時啟動時用檔案鎖，只產生一組。"""
    if 'v' in _vapid_cache:
        return _vapid_cache['v']
    from py_vapid import Vapid01
    from cryptography.hazmat.primitives import serialization
    with open(VAPID_FILE + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(VAPID_FILE):
            with open(VAPID_FILE, encoding='utf-8') as f:
                v = Vapid01.from_pem(json.load(f)['private_pem'].encode())
        else:
            v = Vapid01()
            v.generate_keys()
            fd = os.open(VAPID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'private_pem': v.private_pem().decode()}, f)
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _vapid_cache['v'] = (v, base64.urlsafe_b64encode(raw).rstrip(b'=').decode())
    return _vapid_cache['v']


def web_subs(user):
    return userdata.get(user, 'push_subs') or []


def push_web(user, title, body, url):
    """送到這個帳號訂閱過的每台裝置；失效的訂閱（404/410）順便刪掉。
    回傳 (是否至少一台成功, 錯誤訊息)；沒有訂閱回傳 (None, '')。"""
    subs = web_subs(user)
    if not subs:
        return None, ''
    from pywebpush import webpush, WebPushException
    v, _ = _vapid()
    data = json.dumps({'title': title, 'body': body[:1500], 'url': url}, ensure_ascii=False)
    ok, errors, gone = False, [], set()
    for s in subs:
        try:
            webpush({'endpoint': s['endpoint'], 'keys': s['keys']}, data, vapid_private_key=v,
                    vapid_claims={'sub': VAPID_SUB}, ttl=3600, timeout=10)
            ok = True
        except WebPushException as e:
            code = getattr(e.response, 'status_code', None)
            if code in (404, 410):
                gone.add(s['endpoint'])
            else:
                errors.append(f'{code or ""} {str(e)[:120]}'.strip())
        except Exception as e:
            errors.append(str(e)[:120])
    if gone:
        userdata.put(user, 'push_subs', [s for s in web_subs(user) if s['endpoint'] not in gone])
    if ok or not errors:
        return (ok or None), ''
    return False, '手機／瀏覽器通知發送失敗：' + errors[0]


# ── 對外介面 ──────────────────────────────────────────────────────────
def channels(user):
    token, uid = line_creds(user)
    return {'line': bool(token and uid), 'web': len(web_subs(user))}


def ready(user):
    c = channels(user)
    return c['line'] or c['web'] > 0


def _record(user, results):
    st = userdata.get(user, 'notify_status') or {}
    for ch, (ok, err) in results.items():
        if ok is not None:
            st[ch] = {'ok': ok, 'error': err, 'ts': _now()}
    userdata.put(user, 'notify_status', st)


def send(user, text, url='/tw/board'):
    """送一則通知到這個帳號的所有管道。第一行當標題。
    回傳 (是否至少一個管道成功, 錯誤訊息（各管道的失敗原因，用「；」串起來）)。"""
    title, _, body = text.partition('\n')
    results = {'line': push_line(user, text), 'web': push_web(user, title, body or title, url)}
    _record(user, results)
    ok = any(r[0] for r in results.values())
    return ok, '；'.join(r[1] for r in results.values() if r[1])


def status(user):
    st = userdata.get(user, 'notify_status') or {}
    c = channels(user)
    if c['line'] and 'line' not in st:
        # 還沒發過（或剛升級前就存好的金鑰）：先驗證一次，錯了馬上就能在畫面上看到
        err = check_line(*line_creds(user))
        if err is not None:
            st['line'] = {'ok': not err, 'error': err, 'ts': ''}
            userdata.put(user, 'notify_status', st)
    return {
        'line': dict(st.get('line', {}), configured=c['line']) if c['line'] else {'configured': False},
        'web': dict(st.get('web', {}), devices=c['web']),
        'errors': [s['error'] for ch, s in st.items()
                   if not s.get('ok') and s.get('error') and (ch != 'line' or c['line'])],
    }


def save_line(user, token, uid):
    """儲存 LINE 金鑰，先向 LINE 驗證；回傳錯誤訊息（空字串 = 成功）。"""
    token, uid = token.strip(), uid.strip()
    if token or uid:
        if not token or not uid:
            return 'Channel access token 與 User ID 都要填'
        err = check_line(token, uid)
        if err:
            return err
    userdata.put(user, 'line', {'token': token, 'user_id': uid})
    st = userdata.get(user, 'notify_status') or {}
    st.pop('line', None)
    userdata.put(user, 'notify_status', st)
    return ''


# ── API ───────────────────────────────────────────────────────────────
@bp.route('/api/notify/status')
def api_status():
    out = status(userdata.current())
    out['vapid_public'] = _vapid()[1]
    return jsonify(out)


@bp.route('/api/notify/subscribe', methods=['POST'])
def api_subscribe():
    b = request.get_json(silent=True) or {}
    ep, keys = b.get('endpoint'), b.get('keys') or {}
    if not isinstance(ep, str) or not ep.startswith('https://') or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify(error='訂閱資料不完整'), 400
    user = userdata.current()
    subs = [s for s in web_subs(user) if s['endpoint'] != ep]
    subs.append({'endpoint': ep, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']},
                 'ua': request.headers.get('User-Agent', '')[:200], 'added': _now()})
    userdata.put(user, 'push_subs', subs[-MAX_SUBS:])
    return jsonify(ok=True, devices=len(subs[-MAX_SUBS:]))


@bp.route('/api/notify/unsubscribe', methods=['POST'])
def api_unsubscribe():
    ep = (request.get_json(silent=True) or {}).get('endpoint')
    user = userdata.current()
    subs = [s for s in web_subs(user) if s['endpoint'] != ep]
    userdata.put(user, 'push_subs', subs)
    return jsonify(ok=True, devices=len(subs))


@bp.route('/api/notify/test', methods=['POST'])
def api_test():
    user = userdata.current()
    if not ready(user):
        return jsonify(error='還沒有設定任何通知管道（LINE 或這台裝置的通知）'), 400
    ok, err = send(user, f'【StockLens】通知測試成功\n{_now()}　收到這則代表通知設定正確。')
    return (jsonify(ok=True, warning=err), 200) if ok else (jsonify(error=err), 400)
