# -*- coding: utf-8 -*-
"""通知：每個帳號的 LINE 推播與手機／瀏覽器推播（Web Push）。

send(user, text) 送到這個帳號設定好的所有管道，每個管道最近一次的結果記在
userdata 'notify_status'，畫面上的錯誤提醒（token 失效、額度用完…）就是讀這份紀錄。
Web Push 的 VAPID 金鑰第一次用到時自動產生，存在 .vapid.json（不進版控）。
"""
import os
import re
import hmac
import json
import fcntl
import base64
import random
import hashlib
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
    """儲存前檢查 LINE 金鑰；回傳錯誤訊息，沒問題回傳空字串。網路錯誤回傳 None（無法判斷）。
    uid 空白代表要用 webhook 綁定，只檢查 token。"""
    errs = []
    if uid and not LINE_UID_RE.match(uid):
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


# ── LINE 每月額度 ─────────────────────────────────────────────────────
# 輕用量方案每月 200 則免費推播，超過就發不出去（429）。發之前先看本月用量，
# 用量以 LINE 官方的 quota/consumption 為準（同一個官方帳號其他地方發的也算），
# 查不到時退回本系統自己的計數。reply（綁定回覆）不算額度。
LINE_CAP_DEFAULT = 200
_usage_cache = {}          # token → (查詢時間, 已用, LINE 的上限或 None)


def _month():
    return dt.datetime.now().strftime('%Y-%m')


def _line_quota(token):
    hit = _usage_cache.get(token)
    if hit and (dt.datetime.now() - hit[0]).total_seconds() < 60:
        return hit[1], hit[2]
    h = {'Authorization': f'Bearer {token}'}
    try:
        used = requests.get('https://api.line.me/v2/bot/message/quota/consumption', timeout=10, headers=h)
        quota = requests.get('https://api.line.me/v2/bot/message/quota', timeout=10, headers=h)
    except requests.RequestException:
        return None, None
    if not used.ok:
        return None, None
    try:
        q = quota.json() if quota.ok else {}
        out = (int(used.json().get('totalUsage', 0)), int(q['value']) if q.get('type') == 'limited' else None)
    except (ValueError, KeyError, TypeError):
        return None, None
    _usage_cache[token] = (dt.datetime.now(), *out)
    return out


def line_usage(user):
    """{'used', 'cap', 'month'}：本月已發幾則、上限幾則。"""
    token, _ = line_creds(user)
    c = userdata.get(user, 'line') or {}
    cap = c.get('cap') if isinstance(c.get('cap'), int) and c.get('cap') > 0 else LINE_CAP_DEFAULT
    local = userdata.get(user, 'line_usage') or {}
    used = local.get('count', 0) if local.get('month') == _month() else 0
    if token:
        official, limit = _line_quota(token)
        if official is not None:
            used = max(used, official)
        if limit:
            cap = min(cap, limit)
    return {'used': used, 'cap': cap, 'month': _month()}


def _count_line(user, token):
    local = userdata.get(user, 'line_usage') or {}
    n = local.get('count', 0) + 1 if local.get('month') == _month() else 1
    userdata.put(user, 'line_usage', {'month': _month(), 'count': n})
    hit = _usage_cache.get(token)
    if hit:
        _usage_cache[token] = (hit[0], hit[1] + 1, hit[2])


def push_line(user, text):
    """回傳 (是否成功, 錯誤訊息)；沒設定 LINE 回傳 (None, '')。"""
    token, uid = line_creds(user)
    if not token or not uid:
        return None, ''
    u = line_usage(user)
    if u['used'] >= u['cap']:
        return False, (f"LINE 本月已發 {u['used']} 則，達每月上限 {u['cap']} 則，這則沒有送 LINE"
                       f"（手機通知照常），下個月 1 日恢復")
    try:
        r = requests.post('https://api.line.me/v2/bot/message/push', timeout=10,
                          headers={'Authorization': f'Bearer {token}'},
                          json={'to': uid, 'messages': [{'type': 'text', 'text': text[:4900]}]})
    except requests.RequestException as e:
        return False, f'連不到 LINE：{e}'
    if r.ok:
        _count_line(user, token)
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
        'line': dict(st.get('line', {}), configured=c['line']) if c['line'] else
                {'configured': False, 'pending': bool(line_creds(user)[0])},
        'web': dict(st.get('web', {}), devices=c['web']),
        'line_usage': line_usage(user) if c['line'] else None,
        'errors': [s['error'] for ch, s in st.items()
                   if not s.get('ok') and s.get('error') and (ch != 'line' or c['line'])],
    }


def save_line(user, token, uid, secret=None):
    """儲存 LINE 金鑰，先向 LINE 驗證；回傳錯誤訊息（空字串 = 成功）。
    User ID 可以不填，改用 webhook 綁定（需要 Channel secret）；secret 為 None 時沿用原本的。"""
    token, uid = token.strip(), uid.strip()
    old = userdata.get(user, 'line') or {}
    secret = old.get('secret', '') if secret is None else secret.strip()
    if token or uid:
        if not token:
            return '請填 Channel access token'
        if not uid and not secret:
            return 'User ID 與 Channel secret 至少填一個（填 secret 可用綁定碼自動取得 User ID）'
        if secret and not re.fullmatch(r'[0-9a-f]{32}', secret):
            return 'Channel secret 格式不對，應為 32 個英數字（在 LINE Developers 的 Basic settings）'
        err = check_line(token, uid)
        if err:
            return err
    else:
        secret = ''
    c = {'token': token, 'user_id': uid, 'secret': secret}
    if token and old.get('cap'):
        c['cap'] = old['cap']
    userdata.put(user, 'line', c)
    st = userdata.get(user, 'notify_status') or {}
    st.pop('line', None)
    userdata.put(user, 'notify_status', st)
    return ''


# ── LINE webhook 綁定 ────────────────────────────────────────────────
# 帳號頁產生 6 碼綁定碼 → 使用者在 LINE 傳給官方帳號 → webhook 收到後把傳訊者的 User ID
# 存進這個帳號。每個帳號用自己的官方帳號，所以用各自的 Channel secret 驗簽來找出是哪個帳號。
BIND_KEY = 'line_bind'
BIND_TTL = 600


def new_bind_code(user):
    code = f'{random.SystemRandom().randrange(10 ** 6):06d}'
    userdata.put(user, BIND_KEY, {'code': code, 'exp': (dt.datetime.now() + dt.timedelta(seconds=BIND_TTL)).isoformat()})
    return code


def _sig_ok(secret, body, sig):
    mac = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(mac, sig or '')


def _reply(token, reply_token, text):
    try:
        requests.post('https://api.line.me/v2/bot/message/reply', timeout=10,
                      headers={'Authorization': f'Bearer {token}'},
                      json={'replyToken': reply_token, 'messages': [{'type': 'text', 'text': text}]})
    except requests.RequestException:
        pass


def handle_webhook(body, sig):
    """回傳 HTTP 狀態碼。驗簽失敗 401；LINE 後台按「Verify」送的空事件也會回 200。"""
    owners = [u for u, c in userdata.users_with('line') if c.get('secret') and _sig_ok(c['secret'], body, sig)]
    if not owners:
        return 401
    try:
        events = json.loads(body or b'{}').get('events', [])
    except ValueError:
        return 400
    for ev in events:
        src = (ev.get('source') or {}).get('userId', '')
        msg = ev.get('message') or {}
        text = (msg.get('text') or '').strip() if msg.get('type') == 'text' else ''
        if not src or not ev.get('replyToken'):
            continue
        for user in owners:            # 同一個官方帳號可能被多個帳號共用，比對綁定碼決定是誰
            b = userdata.get(user, BIND_KEY) or {}
            c = userdata.get(user, 'line') or {}
            if text and b.get('code') == text and b.get('exp', '') > dt.datetime.now().isoformat():
                c['user_id'] = src
                userdata.put(user, 'line', c)
                userdata.delete(user, BIND_KEY)
                st = userdata.get(user, 'notify_status') or {}
                st.pop('line', None)
                userdata.put(user, 'notify_status', st)
                _reply(c['token'], ev['replyToken'], f'✅ 已綁定 StockLens 帳號「{user}」，之後的通知會送到這裡。')
                break
        else:
            if re.fullmatch(r'\d{6}', text):
                _reply(userdata.get(owners[0], 'line')['token'], ev['replyToken'],
                       '綁定碼不正確或已過期，請到 StockLens「帳號 → 通知設定」重新產生。')
    return 200


@bp.route('/api/line/webhook', methods=['POST'])
def line_webhook():
    code = handle_webhook(request.get_data(), request.headers.get('X-Line-Signature', ''))
    return jsonify(ok=code == 200), code


@bp.route('/api/notify/line/cap', methods=['POST'])
def api_line_cap():
    user = userdata.current()
    cap = (request.get_json(silent=True) or {}).get('cap')
    c = userdata.get(user, 'line') or {}
    if not c.get('token'):
        return jsonify(error='請先設定 LINE'), 400
    if not isinstance(cap, int) or not 1 <= cap <= 100000:
        return jsonify(error='每月上限請填 1 以上的整數'), 400
    c['cap'] = cap
    userdata.put(user, 'line', c)
    return jsonify(line_usage(user))


@bp.route('/api/notify/line/bind', methods=['POST'])
def api_line_bind():
    user = userdata.current()
    c = userdata.get(user, 'line') or {}
    if not c.get('token') or not c.get('secret'):
        return jsonify(error='請先填好 Channel access token 與 Channel secret 並儲存'), 400
    return jsonify(code=new_bind_code(user), ttl=BIND_TTL)


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
