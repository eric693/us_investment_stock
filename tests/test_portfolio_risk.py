"""投資組合風險：權重、集中度與相關性警示、風險平價、停損上限。"""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from conftest import login


@pytest.fixture
def pr(board, tmp_path, monkeypatch):
    import fa_scan
    import portfolio_risk
    monkeypatch.setattr(fa_scan, 'DB_PATH', str(tmp_path / 'fa.db'))
    fa_scan.init_db()
    rng = np.random.default_rng(1)
    dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=260)
    base = rng.normal(0, 0.01, len(dates))
    con = sqlite3.connect(board.DB_PATH)
    series = {'1111': np.cumprod(1 + base + rng.normal(0, 0.002, len(dates))) * 100,      # 和 2222 高度相關
              '2222': np.cumprod(1 + base + rng.normal(0, 0.002, len(dates))) * 50,
              '3333': np.cumprod(1 + rng.normal(0, 0.03, len(dates))) * 20}                # 獨立、波動大
    for code, px in series.items():
        con.execute('INSERT INTO stocks VALUES(?,?,?,?,?,?)', (code, f'股{code}', 'TSE', '半導體' if code != '3333' else '航運', 'stock', '2026-01-01'))
        for d, p in zip(dates.strftime('%Y-%m-%d'), px):
            con.execute('INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)', (code, d, p, p * 1.01, p * 0.99, p, 0, 1000))
    con.commit(); con.close()
    flat = pd.Series(np.cumprod(1 + base) * 1000, index=dates)
    monkeypatch.setattr(portfolio_risk, '_yf_close', lambda sym, period='1y': pd.Series(32.0, index=dates) if sym == 'TWD=X' else flat)
    with fa_scan._db() as c:
        c.execute(f'INSERT INTO snaps VALUES({",".join("?" * 19)})', ('2026-01-01', 'tw', '1111', '', '', 1, 70, 70, 50, 0, 0, 0, 0, 'buy', 1, 0, 1, 'mid', 0))
        c.execute(f'INSERT INTO snaps VALUES({",".join("?" * 19)})', ('2026-01-02', 'tw', '1111', '', '', 1, 40, 40, 50, 0, 0, 0, 0, 'hold', 1, 0, 1, 'mid', 0))
    return portfolio_risk, series


def test_risk_report(pr):
    m, series = pr
    holdings = [{'ticker': '1111', 'shares': 10000, 'costPrice': 90}, {'ticker': '2222.TW', 'shares': 1000, 'costPrice': 40},
                {'ticker': '3333', 'shares': 1000, 'costPrice': 20}, {'ticker': '1111', 'shares': 1000, 'costPrice': 100}]
    r, err = m.analyze(holdings)
    assert err is None and r['n'] == 3                                   # 同代碼合併
    w = {o['code']: o for o in r['rows']}
    assert sum(o['weight'] for o in r['rows']) == pytest.approx(100, abs=0.2)
    assert w['1111']['shares'] == 11000 and w['1111']['weight'] > 60
    assert r['pairs'] and {r['pairs'][0]['a'], r['pairs'][0]['b']} == {'1111', '2222'}
    assert w['3333']['rp_weight'] < w['1111']['rp_weight']               # 波動大的建議權重小
    assert w['1111']['rating']['rec'] == 'hold' and w['1111']['rating']['prev'] == 'buy'
    text = ' '.join(r['warnings'])
    assert '單一持股' in text and '半導體' in text and '降評' in text and '相關係數' in text
    assert r['max_dd'] <= 0 and r['vol'] > 0 and r['eff_n'] < 3


def test_risk_api(client, pr):
    login(client, 'amy', 'user12345')
    assert client.post('/api/portfolio/risk', json={'holdings': []}).status_code == 400
    j = client.post('/api/portfolio/risk', json={'holdings': [{'ticker': '3333', 'shares': 100, 'costPrice': 10}]}).get_json()
    assert j['n'] == 1 and j['rows'][0]['weight'] == 100
