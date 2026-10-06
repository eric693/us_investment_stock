# -*- coding: utf-8 -*-
"""登入、帳號與權限。

帳號存在 users.json（不進版控）：
    {"帳號": {"hash": "<werkzeug 雜湊>", "role": "admin" | "user", "ver": 1}}
ver 在改密碼 / 停用時遞增，舊的登入 session 會因版本不符而失效。
舊格式 {"帳號": "<雜湊>"} 讀取時自動視為 admin。
"""
import os
import json
import time
import fcntl
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request, session, url_for,
                   abort)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
SECRET_FILE = os.path.join(BASE_DIR, '.secret_key')
FAIL_FILE = os.path.join(BASE_DIR, '.login_fail.json')
MAX_FAILS, FAIL_WINDOW = 10, 900          # 15 分鐘內錯 10 次就暫停
PUBLIC_ENDPOINTS = {'auth.login', 'static'}

bp = Blueprint('auth', __name__)


# ── 帳號存取 ──────────────────────────────────────────────────────────
_users_cache = {'mtime': None, 'data': {}}


def load_users():
    try:
        mt = os.path.getmtime(USERS_FILE)
    except OSError:
        return {}
    if mt != _users_cache['mtime']:
        try:
            with open(USERS_FILE, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return _users_cache['data']
        _users_cache['data'] = {u: (v if isinstance(v, dict) else {'hash': v, 'role': 'admin', 'ver': 1})
                                for u, v in raw.items()}
        _users_cache['mtime'] = mt
    return _users_cache['data']


def save_users(users):
    tmp = USERS_FILE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)
    _users_cache['mtime'] = None


def set_password(users, username, password, role=None):
    u = users.get(username, {'role': role or 'user', 'ver': 0})
    u['hash'] = generate_password_hash(password)
    u['ver'] = u.get('ver', 0) + 1
    if role:
        u['role'] = role
    users[username] = u


def password_problem(pw):
    if len(pw) < 8:
        return '密碼至少 8 碼'
    if pw.isdigit() or pw.isalpha():
        return '密碼需同時包含英文字母與數字'
    return None


# ── 登入失敗次數（多個 worker 共用一個檔案）────────────────────────────
def _fails(ip, add=False, clear=False):
    now = time.time()
    with open(FAIL_FILE, 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            data = json.loads(f.read() or '{}')
        except ValueError:
            data = {}
        data = {k: [t for t in v if now - t < FAIL_WINDOW] for k, v in data.items()}
        data = {k: v for k, v in data.items() if v}
        if clear:
            data.pop(ip, None)
        elif add:
            data.setdefault(ip, []).append(now)
        f.seek(0)
        f.truncate()
        json.dump(data, f)
        return len(data.get(ip, []))


def current_user():
    """目前登入的帳號資料；session 版本與帳號不符（改過密碼、被刪除）就視為未登入。"""
    name = session.get('user')
    u = load_users().get(name) if name else None
    if not u or session.get('ver') != u.get('ver', 1):
        return None
    return dict(u, name=name)


def login_required(f):
    # 實際的檢查在 _require_login（套用到所有路由）；保留此裝飾器讓既有路由不用改
    return f


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u.get('role') != 'admin':
            if request.path.startswith('/api/'):
                return jsonify(error='需要管理員權限'), 403
            abort(403)
        return f(*a, **kw)
    return wrapper


# ── 路由 ──────────────────────────────────────────────────────────────
@bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        ip = request.headers.get('X-Real-IP', request.remote_addr)
        if _fails(ip) >= MAX_FAILS:
            error = '嘗試次數過多，請 15 分鐘後再試'
        else:
            username = request.form.get('username', '').strip()
            u = load_users().get(username)
            if u and check_password_hash(u['hash'], request.form.get('password', '')):
                session.clear()
                session.permanent = True
                session.update(user=username, ver=u.get('ver', 1), role=u.get('role', 'user'))
                _fails(ip, clear=True)
                nxt = request.args.get('next', '')
                return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/')
            _fails(ip, add=True)
            time.sleep(1)
            error = '帳號或密碼錯誤'
    return render_template('login.html', error=error)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/account', methods=['GET', 'POST'])
def account():
    u = current_user()
    msg = err = None
    if request.method == 'POST':
        users = load_users()
        cur, new, new2 = (request.form.get(k, '') for k in ('current', 'new', 'new2'))
        if not check_password_hash(users[u['name']]['hash'], cur):
            err = '目前密碼不正確'
        elif new != new2:
            err = '兩次輸入的新密碼不同'
        else:
            err = password_problem(new)
        if not err:
            set_password(users, u['name'], new)
            save_users(users)
            session['ver'] = users[u['name']]['ver']   # 本裝置維持登入，其他裝置登出
            msg = '密碼已更新，其他裝置上的登入已失效'
    return render_template('account.html', user=current_user(), msg=msg, err=err)


@bp.route('/admin/users')
@admin_required
def admin_users():
    return render_template('admin_users.html', user=current_user())


@bp.route('/api/admin/users', methods=['GET', 'POST'])
@admin_required
def api_users():
    users = load_users()
    if request.method == 'POST':
        b = request.get_json(silent=True) or {}
        action, name = b.get('action'), str(b.get('username', '')).strip()
        me = current_user()['name']
        admins = [n for n, v in users.items() if v.get('role') == 'admin']
        if action == 'add':
            if not name or not name.replace('_', '').replace('.', '').isalnum() or len(name) > 32:
                return jsonify(error='帳號只能用英數字、底線、句點，最多 32 字'), 400
            if name in users:
                return jsonify(error='帳號已存在'), 400
            p = password_problem(b.get('password', ''))
            if p:
                return jsonify(error=p), 400
            set_password(users, name, b['password'], role='admin' if b.get('role') == 'admin' else 'user')
        elif name not in users:
            return jsonify(error='查無此帳號'), 404
        elif action == 'reset':
            p = password_problem(b.get('password', ''))
            if p:
                return jsonify(error=p), 400
            set_password(users, name, b['password'])
        elif action == 'role':
            role = 'admin' if b.get('role') == 'admin' else 'user'
            if role == 'user' and admins == [name]:
                return jsonify(error='至少要保留一位管理員'), 400
            users[name]['role'] = role
            users[name]['ver'] = users[name].get('ver', 1) + 1    # 權限變更後重新登入
        elif action == 'delete':
            if name == me:
                return jsonify(error='不能刪除自己'), 400
            users.pop(name)
            import userdata
            userdata.delete_user(name)       # 該帳號的個人資料一併刪除
        else:
            return jsonify(error='未知的動作'), 400
        save_users(users)
        if name == me and action in ('reset', 'role') and name in users:
            session['ver'] = users[name]['ver']
            session['role'] = users[name]['role']
    return jsonify([dict(username=n, role=v.get('role', 'user')) for n, v in sorted(users.items())])


# ── 掛到 app ──────────────────────────────────────────────────────────
def _secret_key():
    if not os.path.exists(SECRET_FILE):
        try:
            fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(os.urandom(32).hex())
        except FileExistsError:          # 另一個 worker 剛好同時建立
            time.sleep(0.5)
    with open(SECRET_FILE) as f:
        return f.read().strip()


def init_auth(app):
    app.secret_key = _secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))

    @app.before_request
    def _require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if current_user():
            return None
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify(error='未登入'), 401
        return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))

    @app.context_processor
    def _inject_user():
        return {'current_user': current_user()}

    app.register_blueprint(bp)
