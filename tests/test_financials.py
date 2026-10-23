"""財報分析：指標計算、合理價、建議與警訊降級。"""
import numpy as np
import pandas as pd
import pytest

from conftest import login


def _frame(rows, dates):
    return pd.DataFrame({d: {k: v[i] for k, v in rows.items()} for i, d in enumerate(dates)})


class FakeTicker:
    def __init__(self, growth=0.15, ocf_ratio=1.2, debt=100.0, price=100.0):
        ya = [pd.Timestamp(f'{y}-12-31') for y in (2022, 2023, 2024, 2025)]
        yq = [pd.Timestamp(d) for d in ('2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31', '2026-03-31', '2026-06-30')]
        rev_a = [1000 * (1 + growth) ** i for i in range(4)]
        rev_q = [rev_a[-1] / 4 * (1 + growth) ** (i / 4) for i in range(6)]
        mk = lambda rev: {'Total Revenue': rev, 'Gross Profit': [r * 0.5 for r in rev], 'Operating Income': [r * 0.25 for r in rev],
                          'Net Income': [r * 0.2 for r in rev], 'Diluted EPS': [r * 0.2 / 100 for r in rev], 'EBITDA': [r * 0.3 for r in rev],
                          'EBIT': [r * 0.25 for r in rev], 'Interest Expense': [5.0] * len(rev), 'Tax Rate For Calcs': [0.2] * len(rev)}
        self.income_stmt, self.quarterly_income_stmt = _frame(mk(rev_a), ya), _frame(mk(rev_q), yq)
        cf = lambda rev: {'Operating Cash Flow': [r * 0.2 * ocf_ratio for r in rev], 'Free Cash Flow': [r * 0.15 * ocf_ratio for r in rev],
                          'Stock Based Compensation': [r * 0.01 for r in rev]}
        self.cashflow, self.quarterly_cashflow = _frame(cf(rev_a), ya), _frame(cf(rev_q), yq)
        bs = lambda n: {'Stockholders Equity': [1500.0 + 50 * i for i in range(n)], 'Total Assets': [3000.0] * n,
                        'Total Liabilities Net Minority Interest': [1500.0] * n, 'Total Debt': [debt] * n,
                        'Cash Cash Equivalents And Short Term Investments': [300.0] * n, 'Current Assets': [1200.0] * n,
                        'Current Liabilities': [600.0] * n, 'Working Capital': [600.0] * n, 'Retained Earnings': [900.0] * n,
                        'Invested Capital': [1600.0] * n, 'Long Term Debt': [debt] * n, 'Ordinary Shares Number': [100.0] * n}
        self.balance_sheet, self.quarterly_balance_sheet = _frame(bs(4), ya), _frame(bs(6), yq)
        self.info = {'currentPrice': price, 'sharesOutstanding': 100.0, 'sector': 'Technology', 'currency': 'USD',
                     'targetMeanPrice': price * 1.3, 'numberOfAnalystOpinions': 12, 'recommendationKey': 'buy',
                     'lastFiscalYearEnd': pd.Timestamp('2025-12-31').timestamp(), 'dividendYield': 1.0}
        eps = rev_a[-1] * 0.2 / 100
        self.earnings_estimate = pd.DataFrame({'avg': {'0y': eps * (1 + growth), '+1y': eps * (1 + growth) ** 2},
                                               'growth': {'0y': growth, '+1y': growth}, 'numberOfAnalysts': {'0y': 12, '+1y': 10}})
        self.eps_trend = pd.DataFrame({'current': {'+1y': 1.05}, '90daysAgo': {'+1y': 1.0}})
        self.earnings_history = pd.DataFrame({'surprisePercent': [0.05, 0.03, -0.01, 0.04]})
        self.recommendations_summary = pd.DataFrame([{'strongBuy': 3, 'buy': 6, 'hold': 3, 'sell': 0, 'strongSell': 0}])

    def history(self, **kw):
        idx = pd.date_range('2021-01-31', '2026-06-30', freq='ME')
        return pd.DataFrame({'Close': np.linspace(30, 60, len(idx))}, index=idx)


@pytest.fixture
def fa(board, monkeypatch, tmp_path):
    import financials
    financials._cache.clear()
    monkeypatch.setattr(financials, 'CACHE_DIR', str(tmp_path / 'fa_cache'))
    monkeypatch.setattr(financials, '_us_sector', lambda sector, code: {'name': sector, 'n': 30, 'pe': 25.0, 'fpe': 20.0, 'ev_ebitda': 15.0})
    return financials


def test_quality_company_gets_buy(fa, monkeypatch):
    monkeypatch.setattr(fa.yf, 'Ticker', lambda t: FakeTicker(growth=0.2, price=40.0))
    r, err = fa.analyze('GOOD')
    assert err is None
    keys = {m['key']: m for p in r['pillars'] for m in p['metrics']}
    assert keys['gm']['value'] == pytest.approx(50.0) and keys['om']['value'] == pytest.approx(25.0)
    assert keys['rev_cagr']['value'] == pytest.approx(20.0, abs=0.1)
    assert keys['nd_ebitda']['value'] < 0 and keys['nd_ebitda']['score'] >= 95      # 淨現金
    assert r['quality'] >= 65 and r['fair']['upside'] > 15
    assert r['rec']['level'] == 'buy' and not r['flags']
    assert {m['name'] for m in r['fair']['methods']} == {'本益比法', '現金流折現（DCF）', '分析師目標價'}
    assert r['fair']['buy_below'] == pytest.approx(r['fair']['value'] * 0.85, rel=1e-3)
    assert r['summary']['pros'] and '合理價' in r['summary']['conclusion']


def test_red_flags_downgrade(fa, monkeypatch):
    # 現金流遠低於淨利、高負債 → 有警訊，就算估值便宜也不給買進
    monkeypatch.setattr(fa.yf, 'Ticker', lambda t: FakeTicker(growth=0.2, ocf_ratio=0.3, debt=2500.0, price=30.0))
    r, _ = fa.analyze('RISKY')
    assert any('現金流' in f for f in r['flags']) and any('槓桿' in f or '負債' in f for f in r['flags'])
    assert r['rec']['level'] in ('hold', 'avoid')


def test_overvalued_is_not_buy(fa, monkeypatch):
    monkeypatch.setattr(fa.yf, 'Ticker', lambda t: FakeTicker(growth=0.02, price=150.0))
    r, _ = fa.analyze('PRICEY')
    assert r['fair']['upside'] < -20 and r['rec']['level'] in ('hold', 'avoid')


def test_api(client, fa, monkeypatch):
    monkeypatch.setattr(fa.yf, 'Ticker', lambda t: FakeTicker())
    login(client, 'amy', 'user12345')
    assert client.get('/api/financials/!!').status_code == 404
    j = client.get('/api/financials/GOOD').get_json()
    assert j['rec']['label'] in ('買進', '逢低布局', '持有觀望', '避開') and len(j['pillars']) == 5
    assert client.get('/financials').status_code == 200
