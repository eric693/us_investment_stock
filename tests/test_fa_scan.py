"""財報掃描：最新結果篩選、建議改變、建議成績單的報酬計算。"""
import sqlite3

import pandas as pd
import pytest

from conftest import login


@pytest.fixture
def sc(board, tmp_path, monkeypatch):
    import fa_scan
    monkeypatch.setattr(fa_scan, 'DB_PATH', str(tmp_path / 'fa.db'))
    monkeypatch.setattr(fa_scan, 'STATUS_FILE', str(tmp_path / 'st.json'))
    fa_scan.init_db()
    # 30 個交易日：A 每天漲 1%，B 持平，C 每天跌 1%；2330 用來決定交易日
    dates = pd.bdate_range('2026-01-05', periods=30).strftime('%Y-%m-%d').tolist()
    con = sqlite3.connect(board.DB_PATH)
    for code, g in (('A', 1.01), ('B', 1.0), ('C', 0.99), ('2330', 1.0)):
        for i, d in enumerate(dates):
            con.execute('INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)', (code, d, 0, 0, 0, 100 * g ** i, 0, 1000))
    con.commit(); con.close()
    return fa_scan, dates


def _snap(fs, date, rows):
    with fs._db() as con:
        for code, rec, score, price, buy_below in rows:
            con.execute(f'INSERT INTO snaps VALUES({",".join("?" * 19)})',
                        (date, 'tw', code, code, '半導體', price, score, score, 50, 50, 50, 50, 50, rec, price * 1.2, 20.0, buy_below, 'mid', 0))
        con.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)', ('tw', date, 'x', 'y', len(rows), 0))


def test_track_record_excess_returns(sc):
    fs, dates = sc
    _snap(fs, dates[0], [('A', 'buy', 90, 100, 120), ('B', 'hold', 50, 100, 80), ('C', 'avoid', 20, 100, 80)])
    t = fs.track('tw')
    week = {r['key']: r['cells'][0] for r in t['table']}
    assert week['buy']['n'] == 1 and week['buy']['ret'] == pytest.approx((1.01 ** 5 - 1) * 100, abs=0.01)
    assert week['buy']['excess'] > 0 > week['avoid']['excess'] and week['buy']['win'] == 100
    assert week['below']['n'] == 1                     # 只有 A 低於買進價
    assert t['table'][0]['cells'][3]['n'] == 0         # 6 個月還沒到
    assert t['since'] == dates[0] and t['note'] is None


def test_screen_latest_and_rec_change(client, sc):
    fs, dates = sc
    _snap(fs, dates[0], [('A', 'hold', 60, 100, 90)])
    _snap(fs, dates[1], [('A', 'buy', 80, 100, 120), ('B', 'avoid', 30, 100, 80)])
    s = fs.screen('tw')
    a = next(r for r in s['rows'] if r['code'] == 'A')
    assert s['date'] == dates[1] and a['prev_rec'] == 'hold' and a['below_buy']
    login(client, 'amy', 'user12345')
    assert client.get('/api/fa/screen?market=tw').get_json()['date'] == dates[1]
    assert client.get('/api/fa/track?market=xx').status_code == 400


def test_empty_track(sc):
    fs, _ = sc
    assert fs.track('tw')['table'] == []
