import os
import sys
import sqlite3

import numpy as np
import pandas as pd
import pytest

os.environ['TW_BOARD_NO_BG'] = '1'          # 測試時不啟動背景同步 / 即時輪詢
os.environ['STOCKLENS_SKIP_MIGRATION'] = '1'  # 測試不能搬動正式資料檔
os.environ['SESSION_COOKIE_SECURE'] = '0'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth        # noqa: E402
import tw_board    # noqa: E402


@pytest.fixture
def board(tmp_path, monkeypatch):
    """把 tw_board 的資料檔都指到暫存目錄。"""
    import userdata
    for name, fn in [('DB_PATH', 'tw.db'), ('RT_FILE', 'rt.json'), ('STATUS_FILE', 'st.json')]:
        monkeypatch.setattr(tw_board, name, str(tmp_path / fn))
    monkeypatch.setattr(userdata, 'DB_PATH', str(tmp_path / 'user_data.db'))
    monkeypatch.setattr(userdata, 'BASE_DIR', str(tmp_path))
    tw_board._frames_cache.clear()
    tw_board._list_cache.clear()
    tw_board._rt_cache.update(mtime=0, data=None)
    tw_board._init_db()
    return tw_board


def make_market(tb, n_days=90, n_codes=40, seed=7):
    """隨機漫步的合成行情與法人買賣超，寫進暫存資料庫。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2026-01-05', periods=n_days).strftime('%Y-%m-%d').tolist()
    codes = [str(1101 + i) for i in range(n_codes)]
    con = sqlite3.connect(tb.DB_PATH)
    for c in codes:
        close = 50 * np.exp(np.cumsum(rng.normal(0, 0.025, n_days)))
        high = close * (1 + rng.uniform(0, 0.02, n_days))
        low = close * (1 - rng.uniform(0, 0.02, n_days))
        vol = rng.integers(100, 5000, n_days).astype(float)
        nets = rng.normal(0, 50, (n_days, 3))
        con.execute('INSERT INTO stocks VALUES(?,?,?,?,?,?)', (c, f'股{c}', 'TSE', '測試', 'stock', dates[-1]))
        for i, d in enumerate(dates):
            con.execute('INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)',
                        (c, d, close[i], high[i], low[i], close[i],
                         close[i] - close[i - 1] if i else 0.0, vol[i]))
            f, t, de = nets[i]
            con.execute('INSERT INTO inst VALUES(?,?,?,?,?,?)', (c, d, f, t, de, f + t + de))
    for d in dates:
        for src in tb.SOURCES:
            con.execute('INSERT INTO sync_log VALUES(?,?,?,?)', (d, src, 'ok', d))
    con.commit()
    con.close()
    return dates, codes


@pytest.fixture
def users(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'USERS_FILE', str(tmp_path / 'users.json'))
    monkeypatch.setattr(auth, 'FAIL_FILE', str(tmp_path / 'fail.json'))
    auth._users_cache.update(mtime=None, data={})
    u = {}
    auth.set_password(u, 'boss', 'admin1234', role='admin')
    auth.set_password(u, 'amy', 'user12345', role='user')
    auth.save_users(u)
    return u


@pytest.fixture
def client(users, board, tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'MONITOR_FILE', str(tmp_path / 'monitor.json'))
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def login(client, user, pw):
    return client.post('/login', data={'username': user, 'password': pw})

