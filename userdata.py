# -*- coding: utf-8 -*-
"""每個帳號自己的資料（投資組合、自選股、推播設定、LINE 金鑰、策略…）。

全部存在 user_data.db（不進版控），以 (帳號, key) 為主鍵，值為 JSON。
API 一律用目前登入的帳號存取，帳號之間看不到彼此的資料。
"""
import os
import json
import fcntl
import sqlite3
import datetime as dt

from flask import Blueprint, jsonify, request, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'user_data.db')
SYSTEM = '__system__'

# 原本存在瀏覽器 localStorage 的個人資料；改由伺服器依帳號保存
BROWSER_KEYS = ('pflio_v2', 'wl', 'wl_tw', 'positions_tw', 'monitorList', 'monitor_settings',
                'priceAlerts', 'priceAlerts_tw', 'line_channel_token', 'line_user_id',
                'tw_ma_settings')
MAX_VALUE = 512 * 1024

bp = Blueprint('userdata', __name__)


def _con():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS kv(user TEXT, key TEXT, value TEXT, updated TEXT, '
                'PRIMARY KEY(user, key))')
    return con


def get(user, key, default=None):
    with _con() as con:
        row = con.execute('SELECT value FROM kv WHERE user=? AND key=?', (user, key)).fetchone()
    return json.loads(row[0]) if row else default


def put(user, key, value):
    with _con() as con:
        con.execute('INSERT OR REPLACE INTO kv VALUES(?,?,?,?)',
                    (user, key, json.dumps(value, ensure_ascii=False),
                     dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))


def delete(user, key):
    with _con() as con:
        con.execute('DELETE FROM kv WHERE user=? AND key=?', (user, key))


def delete_user(user):
    with _con() as con:
        con.execute('DELETE FROM kv WHERE user=?', (user,))


def get_many(user, keys):
    with _con() as con:
        q = ','.join('?' * len(keys))
        rows = con.execute(f'SELECT key, value FROM kv WHERE user=? AND key IN ({q})',
                           (user, *keys)).fetchall()
    return {k: json.loads(v) for k, v in rows}


def users_with(key):
    """[(帳號, 值)]：所有存了這個 key 的帳號（背景推播用）。"""
    with _con() as con:
        rows = con.execute('SELECT user, value FROM kv WHERE key=? AND user != ?', (key, SYSTEM)).fetchall()
    return [(u, json.loads(v)) for u, v in rows]


def current():
    return session.get('user')


# ── 瀏覽器資料 API ─────────────────────────────────────────────────────
@bp.route('/api/user/kv', methods=['GET'])
def api_kv_all():
    return jsonify(get_many(current(), BROWSER_KEYS))


@bp.route('/api/user/kv/<key>', methods=['PUT', 'DELETE'])
def api_kv(key):
    if key not in BROWSER_KEYS and key != '_migrated':
        return jsonify(error='不支援的資料項目'), 400
    if request.method == 'DELETE':
        delete(current(), key)
        return jsonify(ok=True)
    body = request.get_json(silent=True) or {}
    value = body.get('value')
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        return jsonify(error='資料格式錯誤或太大'), 400
    put(current(), key, value)
    return jsonify(ok=True)


def page_context():
    """注入頁面：目前帳號的瀏覽器資料，以及是否要把這台瀏覽器的舊資料搬進帳號。"""
    user = current()
    if not user:
        return {}
    data = get_many(user, BROWSER_KEYS + ('_migrated',))
    migrate = '_migrated' not in data and get(SYSTEM, 'legacy_owner') == user
    data.pop('_migrated', None)
    return {'user_kv': data, 'ukv_migrate': migrate}


# ── 把原本共用的資料搬給原帳號（只做一次）────────────────────────────
def migrate_shared_files(owner):
    """改版前的共用檔案（自選股、推播、LINE、盯盤、策略）都屬於當時唯一的帳號 owner。"""
    lock = open(os.path.join(BASE_DIR, '.userdata_migrate.lock'), 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        if get(SYSTEM, 'legacy_owner'):
            return
        put(SYSTEM, 'legacy_owner', owner)

        def take(fname, key, transform=lambda x: x):
            path = os.path.join(BASE_DIR, fname)
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return None
            if key and get(owner, key) is None:
                put(owner, key, transform(data))
            return data

        take('tw_watchlist.json', 'tw_watchlist',
             lambda d: {'groups': [{'name': '自選股', 'codes': d}]} if isinstance(d, list) else d)
        take('tw_alerts.json', 'tw_alerts')
        take('strategies.json', 'strategies')
        mon = take('monitor_config.json', None)
        if mon:
            if mon.get('line_token') or mon.get('line_user_id'):
                put(owner, 'line', {'token': mon.get('line_token', ''), 'user_id': mon.get('line_user_id', '')})
        for fname in ('tw_watchlist.json', 'tw_alerts.json', 'strategies.json'):
            path = os.path.join(BASE_DIR, fname)
            if os.path.exists(path):
                os.replace(path, path + '.migrated')
    finally:
        lock.close()
