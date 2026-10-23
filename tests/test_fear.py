"""恐慌指標：VIX 分級、三種加碼訊號、回測報酬與提醒去重。"""
import numpy as np
import pandas as pd
import pytest

from conftest import login


def _series(vals, start='2020-01-01'):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)), dtype=float)


@pytest.fixture
def fr(board, monkeypatch):
    import fear
    n = 600
    vix = np.full(n, 15.0)
    vix[100:110] = [22, 28, 35, 45, 50, 48, 40, 38, 30, 25]     # 一波恐慌：觸及 20/25/30/40，之後回落
    v3m = np.full(n, 18.0)
    v3m[100:110] = [20, 24, 30, 38, 40, 40, 38, 37, 31, 27]     # 102～106 倒掛，108 起恢復
    px = 100 * np.cumprod(np.r_[1, np.full(n - 1, 1.001)])
    data = {'^VIX': _series(vix), '^VIX3M': _series(v3m), '^GSPC': _series(px), '^TWII': _series(px)}
    monkeypatch.setattr(fear, '_hist', lambda s: data[s])
    fear._bt_cache.clear()
    return fear, data


def test_labels():
    import fear
    assert fear.vix_label(12)[0] == '過度樂觀' and fear.vix_label(19.7)[0] == '平靜' and fear.vix_label(35)[0] == '極度恐懼'


def test_events(fr):
    fear, d = fr
    vix = d['^VIX']
    touch40 = fear._events(vix, 40, 'touch')
    assert [e[0] for e in touch40] == [vix.index[103]]                  # 只在第一次站上 40
    retreat40 = fear._events(vix, 40, 'retreat')
    assert retreat40[0][0] == vix.index[106] and retreat40[0][2] == 50     # 從 50 回落 20%（≤ 40）的第一天
    term = fear._events(vix, None, 'term', d['^VIX3M'])
    assert term and term[0][0] == vix.index[108]                           # VIX 首次回到 VIX3M 之下


def test_backtest_returns(fr):
    fear, _ = fr
    b = fear.backtest('spx')
    row = next(r for r in b['table'] if r['key'] == 'touch_40')
    assert row['n'] == 1 and row['cells'][0]['ret'] == pytest.approx((1.001 ** 21 - 1) * 100, abs=0.05)
    assert b['events']['touch_40'][0]['entry'] > b['events']['touch_40'][0]['date']     # 隔天才進場


def test_alerts_once_per_wave(client, fr, monkeypatch):
    fear, d = fr
    import notify
    import userdata
    sent = []
    monkeypatch.setattr(notify, 'ready', lambda u: True)
    monkeypatch.setattr(notify, 'send', lambda u, t, url='': sent.append(t) or (True, ''))
    login(client, 'amy', 'user12345')
    assert client.post('/api/fear/alerts', json={'thresholds': [40, 99], 'retreat': True}).get_json()['thresholds'] == [40]
    vix = d['^VIX']
    d['^VIX'] = vix.iloc[:104]                          # 剛站上 40 那天
    fear.run_alerts(); fear.run_alerts()
    assert len(sent) == 1 and '站上 40' in sent[0]
    d['^VIX'] = vix.iloc[:108]                          # 從 50 回落到 38
    fear.run_alerts(); fear.run_alerts()
    assert len(sent) == 2 and '回落' in sent[1]
