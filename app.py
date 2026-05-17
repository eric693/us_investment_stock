from flask import Flask, render_template, jsonify, request
import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import time
import requests as _requests
from xml.etree import ElementTree as ET
import urllib.parse
warnings.filterwarnings('ignore')

app = Flask(__name__)

# ── TTL Cache ─────────────────────────────────────────────────────────
_CACHE = {}

def _cache_get(key):
    e = _CACHE.get(key)
    return e['v'] if e and time.time() < e['t'] else None

def _cache_set(key, val, ttl=300):
    _CACHE[key] = {'v': val, 't': time.time() + ttl}

# ── Helpers ───────────────────────────────────────────────────────
def safe_float(v, default=0.0):
    try:
        if v is None: return default
        f = float(v)
        return default if (np.isnan(f) or np.isinf(f)) else f
    except:
        return default

def safe_int(v, default=0):
    try:
        return int(safe_float(v))
    except:
        return default

def safe_div_yield_pct(info):
    """yfinance 對部分台股/ETF 回傳 dividendYield 已是百分比（如 6.65），
    其他股票則是小數（如 0.065）。統一轉為百分比格式回傳。"""
    raw = safe_float(info.get('dividendYield', 0))
    return raw if raw > 1 else raw * 100

# ── Technical Indicators ──────────────────────────────────────────
def calc_macd(close, fast=12, slow=26, sig=9):
    e1 = close.ewm(span=fast, adjust=False).mean()
    e2 = close.ewm(span=slow, adjust=False).mean()
    macd = e1 - e2
    signal = macd.ewm(span=sig, adjust=False).mean()
    return macd, signal, macd - signal

def calc_rsi(close, period=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(period).mean()
    loss = (-d.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_bollinger(close, period=20, std_dev=2):
    ma  = close.rolling(period).mean()
    std = close.rolling(period).std()
    return ma + std_dev * std, ma, ma - std_dev * std

def calc_returns(hist):
    c   = hist['Close']
    cur = safe_float(c.iloc[-1])
    out = {}
    for label, days in [('1W', 5), ('1M', 21), ('3M', 63), ('6M', 126), ('1Y', 252)]:
        if len(c) > days:
            past = safe_float(c.iloc[-(days + 1)])
            out[label] = round((cur / past - 1) * 100, 2) if past else None
        else:
            out[label] = None
    return out

def get_levels(hist):
    h, l = hist['High'], hist['Low']
    return {
        'resistance2': round(safe_float(h.max()), 2),
        'resistance1': round(safe_float(h.rolling(20).max().iloc[-1]), 2),
        'support1':    round(safe_float(l.rolling(20).min().iloc[-1]), 2),
        'support2':    round(safe_float(l.rolling(60).min().iloc[-1]), 2),
    }

# ── Analysis Generators ───────────────────────────────────────────
def gen_conclusions(price, ma5, ma20, ma60, macd, dea, rsi, vol_ratio):
    out = []
    if price > ma5 > ma20 > ma60:
        out.append({'type': 'star',     'text': '三均線多頭完美排列，趨勢強勢向上'})
    elif price > ma20 > ma60:
        out.append({'type': 'positive', 'text': '股價站穩 MA20 均線，中期趨勢偏多'})
    elif price < ma60:
        out.append({'type': 'negative', 'text': '股價跌破 MA60 均線，趨勢偏空需謹慎'})
    else:
        out.append({'type': 'neutral',  'text': '股價在均線間整理，方向待確認'})

    if macd > dea and macd > 0:
        out.append({'type': 'positive', 'text': f'MACD 金叉且在零軸上方，多頭動能強勁（DIF: {macd:.2f}）'})
    elif macd > dea:
        out.append({'type': 'positive', 'text': 'MACD 低位出現金叉，技術面轉強訊號'})
    else:
        out.append({'type': 'warning',  'text': 'MACD 死叉，短期動能偏弱，觀望為主'})

    if vol_ratio >= 2.0:
        out.append({'type': 'positive', 'text': f'成交量爆量（均量 {vol_ratio:.1f}x），資金動能顯著增強'})
    elif vol_ratio >= 1.5:
        out.append({'type': 'positive', 'text': f'成交量放大（均量 {vol_ratio:.1f}x），資金積極流入'})
    else:
        out.append({'type': 'neutral',  'text': f'成交量正常（均量 {vol_ratio:.1f}x），市場觀望情緒'})

    if rsi > 80:
        out.append({'type': 'warning',  'text': f'RSI {rsi:.0f} 極度超買，注意獲利了結回調'})
    elif rsi > 70:
        out.append({'type': 'warning',  'text': f'RSI {rsi:.0f} 進入超買區，短線謹慎追高'})
    elif rsi < 30:
        out.append({'type': 'positive', 'text': f'RSI {rsi:.0f} 超賣區，技術性反彈機會提升'})
    elif 50 <= rsi <= 70:
        out.append({'type': 'positive', 'text': f'RSI {rsi:.0f} 健康多頭區間，上漲動能充足'})
    else:
        out.append({'type': 'neutral',  'text': f'RSI {rsi:.0f} 中性區間，等待方向確認'})

    return out[:5]

def gen_catalysts(price, ma5, ma20, ma60, macd, dea, rsi, vol_ratio, week52h, info):
    cats = []
    if price >= week52h * 0.97:
        cats.append({'num': 1, 'text': '突破或接近52週高點，歷史強勢突破信號',
                     'sub': '價格創新高，市場認可度顯著提升'})
    if macd > dea and macd > 0:
        cats.append({'num': len(cats)+1, 'text': 'MACD 技術面轉強，動能向上',
                     'sub': '金叉在零軸上方，短中期均偏多'})
    if vol_ratio >= 1.5:
        cats.append({'num': len(cats)+1, 'text': f'成交量異常放大（{vol_ratio:.1f}x 均量）',
                     'sub': '機構資金積極布局跡象明顯'})
    if price > ma5 > ma20 > ma60:
        cats.append({'num': len(cats)+1, 'text': '均線多頭排列完整，趨勢強勢',
                     'sub': '短中長期均線支撐，回撤布局機會'})
    target = safe_float(info.get('targetMeanPrice', 0))
    if target > price * 1.1:
        cats.append({'num': len(cats)+1, 'text': f'分析師目標價 ${target:.2f}，具上漲空間',
                     'sub': f'較現價有 {(target/price-1)*100:.0f}% 潛在漲幅'})
    div_yield  = safe_div_yield_pct(info)
    rev_growth = safe_float(info.get('revenueGrowth', 0)) * 100
    eps_growth = safe_float(info.get('earningsGrowth', 0)) * 100
    roe_v      = safe_float(info.get('returnOnEquity', 0)) * 100
    inst_pct_v = safe_float(info.get('heldPercentInstitutions', 0)) * 100
    pm_v       = safe_float(info.get('profitMargins', 0)) * 100
    sector_v   = (info.get('sector', '') or '').lower()

    extras = []

    # 產業特色
    if any(x in sector_v for x in ['technology', 'communication']):
        extras.append({'text': 'AI 與科技浪潮引領，產業成長邏輯明確',
                       'sub': '雲端、AI、數位轉型需求持續擴張，科技龍頭受惠最深'})
    elif 'financial' in sector_v:
        extras.append({'text': '金融業受惠利差環境，獲利能力穩健',
                       'sub': '利率環境有利銀行放款獲利，現金流充沛且防禦性高'})
    elif 'health' in sector_v:
        extras.append({'text': '醫療健康需求剛性，法規壁壘形成護城河',
                       'sub': '人口老齡化與創新藥需求帶動長期成長，政策支持力道強'})
    elif 'consumer' in sector_v:
        extras.append({'text': '消費品牌護城河深厚，定價能力強',
                       'sub': '剛性消費需求支撐獲利穩定，通膨環境中維持利潤率'})
    elif any(x in sector_v for x in ['energy', 'material']):
        extras.append({'text': '原物料供需缺口支撐，週期性回升可期',
                       'sub': '全球供應緊縮推升價格，景氣復甦期彈性大'})
    else:
        extras.append({'text': '產業地位穩固，長期競爭優勢明確',
                       'sub': '市場份額領先，商業模式持續優化，具長期投資價值'})

    # 成長動能
    if rev_growth >= 20:
        extras.append({'text': f'營收年增 {rev_growth:.0f}%，成長動能強勁',
                       'sub': '高速成長驗證市場需求，估值重估空間持續擴大'})
    elif eps_growth >= 20:
        extras.append({'text': f'EPS 年增 {eps_growth:.0f}%，獲利加速擴張',
                       'sub': '獲利成長超預期，帶動本益比上修，成長邏輯持續兌現'})
    elif roe_v >= 20:
        extras.append({'text': f'ROE {roe_v:.0f}%，資本配置效率優異',
                       'sub': '高股東報酬率顯示管理層創值能力強，具長期複利投資價值'})
    elif pm_v >= 15:
        extras.append({'text': f'淨利率 {pm_v:.0f}%，獲利品質優異',
                       'sub': '高利潤率反映定價能力與成本控制到位，護城河深厚'})
    else:
        extras.append({'text': '財報週期臨近，業績催化持續關注',
                       'sub': '收入成長與利潤率改善趨勢值得追蹤'})

    # 配息 / 成長型
    if div_yield >= 3.0:
        extras.append({'text': f'殖利率 {div_yield:.1f}%，股息收益具吸引力',
                       'sub': '穩定配息在當前利率環境中具防禦特性，吸引收益型投資人'})
    elif div_yield > 0:
        extras.append({'text': f'殖利率 {div_yield:.1f}%，維持配息政策',
                       'sub': '穩定現金股利反映公司現金流健康'})
    else:
        extras.append({'text': '成長型公司，獲利持續再投入擴張',
                       'sub': '保留盈餘用於業務擴張與研發投入，聚焦長期資本增值'})

    # 機構籌碼
    if inst_pct_v >= 60:
        extras.append({'text': f'機構持股 {inst_pct_v:.0f}%，籌碼高度集中穩固',
                       'sub': '高機構持股代表長線資金看好，籌碼穩定不易恐慌賣壓'})
    elif inst_pct_v >= 30:
        extras.append({'text': f'機構持股 {inst_pct_v:.0f}%，法人認同度佳',
                       'sub': '機構資金積極布局，籌碼結構穩健，主力護盤意願強'})
    else:
        extras.append({'text': '機構動向值得持續追蹤',
                       'sub': '機構進出往往領先大盤，追蹤持倉變化可掌握主力意圖'})

    while len(cats) < 4:
        cats.append({'num': len(cats)+1, **extras[len(cats) % len(extras)]})
    return cats[:4]

def gen_investment_value(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v,
                         pe, fwd_pe, roe, profit_margin, rev_growth, eps_growth,
                         beta, debt_equity, vol_ratio):
    s = {}
    # Momentum (0-4)
    if price > ma5 > ma20 > ma60 and macd_v > dea_v and macd_v > 0:
        s['momentum'] = 4
    elif price > ma5 > ma20 > ma60:
        s['momentum'] = 3
    elif price > ma20 > ma60:
        s['momentum'] = 2
    elif price > ma60:
        s['momentum'] = 1
    else:
        s['momentum'] = 0

    # Valuation (0-4)
    ref_pe = fwd_pe if fwd_pe and fwd_pe > 0 else (pe if pe and pe > 0 else 0)
    if   ref_pe <= 0:  s['valuation'] = 2
    elif ref_pe < 15:  s['valuation'] = 4
    elif ref_pe < 25:  s['valuation'] = 3
    elif ref_pe < 40:  s['valuation'] = 2
    elif ref_pe < 60:  s['valuation'] = 1
    else:              s['valuation'] = 0

    # Growth (0-4)
    avg_g = (rev_growth + eps_growth) / 2 if eps_growth else rev_growth
    if   avg_g > 30: s['growth'] = 4
    elif avg_g > 15: s['growth'] = 3
    elif avg_g > 5:  s['growth'] = 2
    elif avg_g > 0:  s['growth'] = 1
    else:            s['growth'] = 0

    # Financial health (0-4)
    h = 2
    if roe > 20:           h += 1
    elif roe < 0:          h -= 1
    if profit_margin > 15: h += 1
    elif profit_margin < 0:h -= 1
    if debt_equity > 200:  h -= 1
    elif debt_equity < 50: h += 1
    s['health'] = max(0, min(4, h))

    def grade(v):
        return 'A+' if v >= 4 else 'A' if v == 3 else 'B' if v == 2 else 'C' if v == 1 else 'D'

    total = sum(s.values()); pct = total / 16
    if   pct >= 0.75: sig, sig_cn, sig_cls = 'STRONG BUY', '強烈買入', 'sv-strong-buy'
    elif pct >= 0.60: sig, sig_cn, sig_cls = 'BUY',        '買入',     'sv-buy'
    elif pct >= 0.45: sig, sig_cn, sig_cls = 'HOLD',       '持有',     'sv-hold'
    elif pct >= 0.30: sig, sig_cn, sig_cls = 'CAUTION',    '觀望',     'sv-caution'
    else:             sig, sig_cn, sig_cls = 'AVOID',      '迴避',     'sv-avoid'

    strengths, weaknesses = [], []
    if s['momentum'] >= 3:  strengths.append('技術趨勢強勁，均線多頭排列完整')
    if s['growth']   >= 3:  strengths.append(f'高速成長，營收 +{rev_growth:.0f}% / EPS +{eps_growth:.0f}%')
    if s['valuation']>= 3:  strengths.append(f'估值合理，預期本益比 {ref_pe:.0f}x 具吸引力')
    if s['health']   >= 3:  strengths.append(f'財務健康，ROE {roe:.0f}%，淨利率 {profit_margin:.0f}%')
    if vol_ratio >= 1.5:    strengths.append(f'量能放大（{vol_ratio:.1f}x），機構積極介入')
    if rsi_v < 40:          strengths.append(f'RSI {rsi_v:.0f} 低檔，技術性反彈空間大')

    if s['valuation'] <= 1 and ref_pe > 0: weaknesses.append(f'估值偏高（本益比 {ref_pe:.0f}x），需業績持續兌現')
    if rsi_v > 70:          weaknesses.append(f'RSI {rsi_v:.0f} 超買，短線追高風險')
    if s['growth'] <= 1:    weaknesses.append('成長動能偏弱，需觀察業績轉機')
    if debt_equity > 150:   weaknesses.append(f'負債比 {debt_equity:.0f}% 偏高，財務槓桿風險')
    if s['momentum'] <= 1:  weaknesses.append('趨勢偏弱，建議等待均線翻多再布局')

    return {
        'signal':   sig,
        'signalCn': sig_cn,
        'signalCls': sig_cls,
        'score':    round(pct * 100),
        'grades': {
            'momentum':  grade(s['momentum']),
            'valuation': grade(s['valuation']),
            'growth':    grade(s['growth']),
            'health':    grade(s['health']),
        },
        'strengths':  strengths[:3],
        'weaknesses': weaknesses[:2],
    }

def gen_etf_invest_value(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v, vol_ratio, info):
    """ETF 專用投資評分：動能 / 費用率 / 績效報酬 / 規模安全性"""
    s = {}

    # Momentum (0-4) — 技術面，ETF 同樣適用
    if price > ma5 > ma20 > ma60 and macd_v > dea_v and macd_v > 0:
        s['momentum'] = 4
    elif price > ma5 > ma20 > ma60:
        s['momentum'] = 3
    elif price > ma20 > ma60:
        s['momentum'] = 2
    elif price > ma60:
        s['momentum'] = 1
    else:
        s['momentum'] = 0

    # Expense ratio (0-4) — 費用率越低越好
    er = safe_float(info.get('annualReportExpenseRatio', info.get('totalExpenseRatio', 0)))
    if er > 1: er /= 100
    er_pct = er * 100
    if   er_pct <= 0:    s['expense'] = 2        # 無資料，中立
    elif er_pct < 0.15:  s['expense'] = 4
    elif er_pct < 0.35:  s['expense'] = 3
    elif er_pct < 0.60:  s['expense'] = 2
    elif er_pct < 1.00:  s['expense'] = 1
    else:                s['expense'] = 0

    # Return performance (0-4) — 以 3 年平均報酬為主，無則用 YTD
    ret3y = safe_float(info.get('threeYearAverageReturn', 0)) * 100
    ytd   = safe_float(info.get('ytdReturn', 0)) * 100
    ref_ret = ret3y if ret3y != 0 else ytd
    if   ref_ret > 20:  s['perf'] = 4
    elif ref_ret > 10:  s['perf'] = 3
    elif ref_ret > 0:   s['perf'] = 2
    elif ref_ret > -10: s['perf'] = 1
    else:               s['perf'] = 0

    # AUM size (0-4) — 規模太小有清算風險
    ta = safe_float(info.get('totalAssets', 0))
    if   ta >= 5e10:  s['size'] = 4     # ≥ 500 億
    elif ta >= 1e10:  s['size'] = 3     # ≥ 100 億
    elif ta >= 1e9:   s['size'] = 2     # ≥ 10 億
    elif ta >= 1e8:   s['size'] = 1     # ≥ 1 億
    else:             s['size'] = 0

    def grade(v):
        return 'A+' if v >= 4 else 'A' if v == 3 else 'B' if v == 2 else 'C' if v == 1 else 'D'

    total = sum(s.values()); pct = total / 16
    if   pct >= 0.75: sig, sig_cn, sig_cls = 'STRONG BUY', '強烈買入', 'sv-strong-buy'
    elif pct >= 0.60: sig, sig_cn, sig_cls = 'BUY',        '買入',     'sv-buy'
    elif pct >= 0.45: sig, sig_cn, sig_cls = 'HOLD',       '持有',     'sv-hold'
    elif pct >= 0.30: sig, sig_cn, sig_cls = 'CAUTION',    '觀望',     'sv-caution'
    else:             sig, sig_cn, sig_cls = 'AVOID',      '迴避',     'sv-avoid'

    strengths, weaknesses = [], []
    if s['momentum'] >= 3: strengths.append('技術趨勢強勁，均線多頭排列完整')
    if s['expense']  >= 3: strengths.append(f'費用率 {er_pct:.2f}%，長期持有成本低廉')
    if s['perf']     >= 3: strengths.append(f'3 年平均報酬 {ret3y:.1f}%，長期績效優異' if ret3y else f'今年報酬 {ytd:.1f}%，績效良好')
    if s['size']     >= 3: strengths.append(f'基金規模 {ta/1e8:.0f} 億，流動性充裕')
    if rsi_v < 40:         strengths.append(f'RSI {rsi_v:.0f} 低檔，技術性反彈機會提升')

    if s['momentum'] <= 1: weaknesses.append('技術趨勢偏弱，建議等待均線翻多再布局')
    if s['expense']  <= 1: weaknesses.append(f'費用率 {er_pct:.2f}% 偏高，長期複利將顯著侵蝕報酬')
    if s['perf']     <= 1: weaknesses.append('近期績效偏弱，需觀察是否相對大盤落後')
    if s['size']     <= 1: weaknesses.append('基金規模偏小，存在流動性不足或清算風險')
    if rsi_v > 70:         weaknesses.append(f'RSI {rsi_v:.0f} 超買，短線追高需謹慎')

    return {
        'signal':    sig,
        'signalCn':  sig_cn,
        'signalCls': sig_cls,
        'score':     round(pct * 100),
        'grades': {
            'momentum': grade(s['momentum']),
            'expense':  grade(s['expense']),
            'perf':     grade(s['perf']),
            'size':     grade(s['size']),
        },
        'strengths':  strengths[:3],
        'weaknesses': weaknesses[:2],
    }


def gen_risks(price, ma20, rsi, vol_ratio, week52h, pe=0, fwd_pe=0, beta=1.0, debt_equity=0, sector=''):
    risks = []
    from_high = (price - week52h) / week52h * 100 if week52h > 0 else 0
    ref_pe = fwd_pe if fwd_pe > 0 else pe

    # Valuation risk
    if ref_pe > 60:
        risks.append({'level':'high',   'category':'估值風險', 'text':f'本益比 {ref_pe:.0f}x 極高，一旦業績不如預期將面臨大幅估值修正，建議分批布局'})
    elif ref_pe > 35:
        risks.append({'level':'medium', 'category':'估值風險', 'text':f'本益比 {ref_pe:.0f}x 偏高，成長需持續兌現以支撐目前股價'})

    # Technical risk
    if rsi > 75:
        risks.append({'level':'high',   'category':'技術風險', 'text':f'RSI {rsi:.0f} 嚴重超買，技術面高度過熱，短線回調風險極高'})
    elif rsi > 70:
        risks.append({'level':'medium', 'category':'技術風險', 'text':f'RSI {rsi:.0f} 進入超買區，短線追高需謹慎，建議等待拉回'})

    if from_high > -5:
        risks.append({'level':'medium', 'category':'技術風險', 'text':f'股價距52週高點僅 {abs(from_high):.1f}%，面臨歷史強壓力區，突破需大量確認'})

    # Trend risk
    if price < ma20:
        risks.append({'level':'high',   'category':'趨勢風險', 'text':'股價跌破 MA20 均線，中期趨勢可能轉弱，建議降低部位等待均線翻多'})

    # Market risk
    if beta > 1.5:
        risks.append({'level':'medium', 'category':'市場風險', 'text':f'Beta {beta:.1f}，波動性高於大盤 {(beta-1)*100:.0f}%，市場修正時跌幅將顯著放大'})

    # Financial risk
    if debt_equity > 200:
        risks.append({'level':'high',   'category':'財務風險', 'text':f'負債股東權益比 {debt_equity:.0f}%，財務槓桿偏高，升息或景氣下行壓力大'})
    elif debt_equity > 100:
        risks.append({'level':'medium', 'category':'財務風險', 'text':f'負債比 {debt_equity:.0f}%，需關注現金流與利息覆蓋能力'})

    # Chip / volume risk
    if vol_ratio > 3.5:
        risks.append({'level':'medium', 'category':'籌碼風險', 'text':f'成交量爆量（{vol_ratio:.1f}x 均量），短期獲利了結賣壓可能增加，注意籌碼鬆動'})

    # Macro & business risks — sector-aware
    sector_l = (sector or '').lower()
    if any(x in sector_l for x in ['technology', 'communication', 'semiconductor']):
        risks.append({'level':'medium', 'category':'總經風險',
                      'text':'聯準會利率政策與通膨數據仍具不確定性，科技股估值對利率變化敏感度較高'})
    else:
        risks.append({'level':'medium', 'category':'總經風險',
                      'text':'聯準會利率政策走向與通膨數據仍具不確定性，需密切追蹤總體環境變化'})
    if any(x in sector_l for x in ['technology', 'communication']):
        risks.append({'level':'low', 'category':'業務風險',
                      'text':'科技迭代加速，競爭格局快速演變，財報不如預期將引發估值修正'})
    elif 'financial' in sector_l:
        risks.append({'level':'low', 'category':'業務風險',
                      'text':'信用風險與壞帳率變化為主要不確定因素，需追蹤貸款品質與資本適足率'})
    else:
        risks.append({'level':'low', 'category':'業務風險',
                      'text':'市場競爭加劇，財報不如預期或展望保守將引發短期大幅波動'})
    risks.append({'level':'low', 'category':'地緣風險',
                  'text':'中美貿易摩擦與地緣政治緊張局勢可能影響供應鏈布局與市場情緒'})

    return risks[:6]

def gen_strategy(price, ma5, ma20, ma60, rsi, levels, target_price=0):
    stop = max(levels['support1'] * 0.97, price * 0.90)
    if price > ma20 and rsi < 70:
        long_t  = f'逢回布局，回測 MA20（${ma20:.2f}）附近加倉，止損設 MA60（${ma60:.2f}）下方 3%'
        swing_t = f'波段操作：突破近期高點 ${levels["resistance1"]:.2f} 後加碼，回踩 MA20 止損'
        short_t = f'短線留意支撐位 ${levels["support1"]:.2f} 附近反彈機會，嚴格設止損'
    else:
        long_t  = f'等待股價站穩 MA60（${ma60:.2f}）後再布局，降低進場風險'
        swing_t = f'等待回測 MA20（${ma20:.2f}）確認支撐後入場，止損設前低'
        short_t = f'技術面偏弱，觀望為主，等待均線金叉信號再行動'
    # 多頭目標：優先使用分析師目標價，否則取最近壓力位上方 5%
    if target_price > price * 1.05:
        bull_t = round(target_price, 1)
    else:
        bull_t = round(levels['resistance1'] * 1.05, 1)
    # 中性目標：最近壓力位
    neutral_t = round(levels['resistance1'], 1)
    # 空頭目標：最近支撐位下方 3%
    bear_t = round(levels['support1'] * 0.97, 1)
    return {
        'long': long_t, 'swing': swing_t, 'short': short_t,
        'stopLoss':      round(stop, 2),
        'bullTarget':    bull_t,
        'neutralTarget': neutral_t,
        'bearTarget':    bear_t,
    }

def gen_tw_strategy(price, ma5, ma20, ma60, rsi, levels, week52h, week52l, info):
    """
    台股目標價採 5 指標交叉驗證：
      ① 分析師目標價   targetMeanPrice（有資料時優先）
      ② 52 週高點      week52h（突破後往上空間）
      ③ 本益比推算     forwardEps × trailingPE（基本面合理價）
      ④ 殖利率還原     dividendRate / 目標殖利率（高息股下檔保護）
      ⑤ 技術壓力/支撐  resistance1 / support1（技術面目標）
    """
    pe           = safe_float(info.get('trailingPE', 0))
    fwd_eps      = safe_float(info.get('forwardEps', 0))
    trailing_eps = safe_float(info.get('trailingEps', 0))
    div_rate     = safe_float(info.get('dividendRate', 0))
    analyst_t    = safe_float(info.get('targetMeanPrice', 0))
    r1           = levels['resistance1']
    s1           = levels['support1']

    # ── 多頭目標（由高至低優先取用） ──────────────────────────────
    bull_candidates = []
    # ① 分析師目標價
    if analyst_t > price * 1.02:
        bull_candidates.append(analyst_t)
    # ② 52週高點突破後上方 3%
    if week52h > price * 1.01:
        bull_candidates.append(week52h * 1.03)
    # ③ 本益比推算（以預估 EPS 優先，無則用 trailing；PE 保守上限 30x）
    eps = fwd_eps if fwd_eps > 0 else trailing_eps
    if eps > 0 and pe > 0:
        bull_candidates.append(eps * min(pe * 1.1, 30))
    # ⑤ fallback：技術壓力位上方 5%
    bull_candidates.append(r1 * 1.05)

    bull_t = round(max(c for c in bull_candidates if c > price * 1.01), 1) \
             if any(c > price * 1.01 for c in bull_candidates) \
             else round(r1 * 1.05, 1)

    # ── 中性目標 ───────────────────────────────────────────────────
    # ③ EPS × 當前 PE 為基本面合理價，無則用技術壓力位
    if eps > 0 and pe > 0:
        neutral_t = round(eps * pe, 1)
    else:
        neutral_t = round(r1, 1)

    # ── 空頭目標（下檔保護） ───────────────────────────────────────
    # ⑤ 技術支撐下方 3%
    bear_t = round(s1 * 0.97, 1)
    # ④ 高息股：殖利率還原至 7%（超過 7% 殖利率通常為強支撐）
    if div_rate > 0:
        yield_floor = round(div_rate / 0.07, 1)
        bear_t = max(bear_t, yield_floor)

    # ── 操作策略文字 ───────────────────────────────────────────────
    stop = max(s1 * 0.97, price * 0.90)
    if price > ma20 and rsi < 70:
        long_t  = f'逢回布局，回測 MA20（{ma20:.2f}）附近加倉，止損設 MA60（{ma60:.2f}）下方 3%'
        swing_t = f'波段操作：突破近期高點 {r1:.2f} 後加碼，回踩 MA20 止損'
        short_t = f'短線留意支撐位 {s1:.2f} 附近反彈機會，嚴格設止損'
    else:
        long_t  = f'等待股價站穩 MA60（{ma60:.2f}）後再布局，降低進場風險'
        swing_t = f'等待回測 MA20（{ma20:.2f}）確認支撐後入場，止損設前低'
        short_t = f'技術面偏弱，觀望為主，等待均線金叉信號再行動'

    return {
        'long': long_t, 'swing': swing_t, 'short': short_t,
        'stopLoss':      round(stop, 2),
        'bullTarget':    bull_t,
        'neutralTarget': neutral_t,
        'bearTarget':    bear_t,
    }


# ── Routes ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/market')
def get_market():
    cached = _cache_get('market')
    if cached: return jsonify(cached)
    syms = {
        'vix':    '^VIX',
        'sp500':  '^GSPC',
        'nasdaq': '^IXIC',
        'dow':    '^DJI',
        'gold':   'GC=F',
        'dxy':    'DX-Y.NYB',
    }
    result = {}
    for key, sym in syms.items():
        try:
            h = yf.Ticker(sym).history(period='2d')
            if len(h) >= 2:
                cur  = safe_float(h['Close'].iloc[-1])
                prev = safe_float(h['Close'].iloc[-2])
                pct  = (cur / prev - 1) * 100 if prev else 0
                result[key] = {'v': round(cur, 2), 'pct': round(pct, 2)}
            elif len(h) == 1:
                result[key] = {'v': round(safe_float(h['Close'].iloc[-1]), 2), 'pct': 0}
            else:
                result[key] = None
        except:
            result[key] = None

    vix_val = (result.get('vix') or {}).get('v', 20)
    if   vix_val < 15: label, cls = '極度貪婪', 'greed-hi'
    elif vix_val < 20: label, cls = '貪婪',     'greed'
    elif vix_val < 25: label, cls = '中性',     'neutral-m'
    elif vix_val < 30: label, cls = '恐懼',     'fear'
    else:              label, cls = '極度恐懼', 'fear-hi'
    result['vixLabel'] = label
    result['vixCls']   = cls
    _cache_set('market', result, ttl=60)
    return jsonify(result)


@app.route('/api/fundamentals/<ticker>')
def get_fundamentals(ticker):
    ticker = ticker.upper().strip()
    cached = _cache_get(f'fund:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock  = yf.Ticker(ticker)
        info   = stock.info

        # ── Cash Flow ──
        ocf_val = fcf_val = 0
        try:
            cf = stock.cashflow
            if cf is not None and not cf.empty:
                for lbl in ['Operating Cash Flow', 'Total Cash From Operating Activities']:
                    if lbl in cf.index:
                        ocf_val = safe_float(cf.loc[lbl].iloc[0]); break
                for lbl in ['Free Cash Flow']:
                    if lbl in cf.index:
                        fcf_val = safe_float(cf.loc[lbl].iloc[0]); break
                if fcf_val == 0 and ocf_val != 0:
                    for lbl in ['Capital Expenditure', 'Capital Expenditures']:
                        if lbl in cf.index:
                            fcf_val = ocf_val + safe_float(cf.loc[lbl].iloc[0]); break
        except:
            pass

        # ── Institutional holders ──
        top_holders = []
        try:
            ih = stock.institutional_holders
            if ih is not None and not ih.empty:
                cols = [str(c) for c in ih.columns]
                # Find the holder name column (non-numeric, non-date column)
                name_col = next((c for c in cols if 'holder' in c.lower() or 'institution' in c.lower()), None)
                pct_col  = next((c for c in cols if 'pct' in c.lower() or '%' in c or 'out' in c.lower()), None)
                val_col  = next((c for c in cols if 'value' in c.lower()), None)
                if name_col:
                    for _, row in ih.head(5).iterrows():
                        holder = str(row[name_col])
                        if holder and holder != 'nan' and not holder[:4].isdigit():
                            pct = safe_float(row[pct_col]) if pct_col else 0
                            val = safe_float(row[val_col]) if val_col else 0
                            pct_disp = round(pct * 100, 2) if pct < 1 else round(pct, 2)
                            top_holders.append({
                                'holder': holder[:35],
                                'pct':    pct_disp,
                                'value':  round(val / 1e9, 2),
                            })
        except:
            pass

        # ── Earnings date ──
        earnings_date = None
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                col = cal.columns[0]
                earnings_date = str(col.date()) if hasattr(col, 'date') else str(col)[:10]
        except:
            pass

        mktcap = safe_float(info.get('marketCap', 0))
        fcf_yield = round(fcf_val / mktcap * 100, 2) if mktcap and fcf_val else 0
        price = safe_float(info.get('currentPrice', info.get('regularMarketPrice', 0)))
        pfcf = round(mktcap / fcf_val, 1) if fcf_val and fcf_val > 0 else None

        result = {
            'ticker':       ticker,
            'ocf':          round(ocf_val / 1e9, 2),
            'fcf':          round(fcf_val / 1e9, 2),
            'fcfYield':     fcf_yield,
            'pfcf':         pfcf,
            'debtEquity':   round(safe_float(info.get('debtToEquity', 0)), 1),
            'currentRatio': round(safe_float(info.get('currentRatio', 0)), 2),
            'roe':          round(safe_float(info.get('returnOnEquity', 0)) * 100, 1),
            'roa':          round(safe_float(info.get('returnOnAssets', 0)) * 100, 1),
            'profitMargin': round(safe_float(info.get('profitMargins', 0)) * 100, 1),
            'grossMargin':  round(safe_float(info.get('grossMargins', 0)) * 100, 1),
            'instPct':      round(safe_float(info.get('heldPercentInstitutions', 0)) * 100, 1),
            'insiderPct':   round(safe_float(info.get('heldPercentInsiders', 0)) * 100, 1),
            'shortRatio':   round(safe_float(info.get('shortRatio', 0)), 1),
            'shortPct':     round(safe_float(info.get('shortPercentOfFloat', 0)) * 100, 2),
            'earningsDate': earnings_date,
            'epsEst':       round(safe_float(info.get('forwardEps', 0)), 2),
            'revGrowth':    round(safe_float(info.get('revenueGrowth', 0)) * 100, 1),
            'epsGrowth':    round(safe_float(info.get('earningsGrowth', 0)) * 100, 1),
            'topHolders':   top_holders,
        }
        _cache_set(f'fund:{ticker}', result)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    cached = _cache_get(f'stock:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock  = yf.Ticker(ticker)
        info   = stock.info
        hist   = stock.history(period='1y')

        if hist.empty:
            return jsonify({'error': f'找不到股票 {ticker}，請確認代碼是否正確'}), 404

        # ── Indicators ──
        hist['MA5']  = hist['Close'].rolling(5).mean()
        hist['MA20'] = hist['Close'].rolling(20).mean()
        hist['MA60'] = hist['Close'].rolling(60).mean()

        macd_s, sig_s, hist_s = calc_macd(hist['Close'])
        hist['MACD']     = macd_s
        hist['Signal']   = sig_s
        hist['MACDHist'] = hist_s
        hist['RSI']      = calc_rsi(hist['Close'])

        bb_upper, bb_mid, bb_lower = calc_bollinger(hist['Close'])
        hist['BB_upper'] = bb_upper
        hist['BB_mid']   = bb_mid
        hist['BB_lower'] = bb_lower

        # ── Core values ──
        price = safe_float(hist['Close'].iloc[-1])
        prev  = safe_float(hist['Close'].iloc[-2])
        change     = price - prev
        change_pct = change / prev * 100 if prev else 0

        ma5    = safe_float(hist['MA5'].iloc[-1])
        ma20   = safe_float(hist['MA20'].iloc[-1])
        ma60   = safe_float(hist['MA60'].iloc[-1])
        macd_v = safe_float(hist['MACD'].iloc[-1])
        dea_v  = safe_float(hist['Signal'].iloc[-1])
        macd_h = safe_float(hist['MACDHist'].iloc[-1])
        rsi_v  = safe_float(hist['RSI'].iloc[-1])

        avg_vol   = safe_float(hist['Volume'].rolling(20).mean().iloc[-1])
        curr_vol  = safe_float(hist['Volume'].iloc[-1])
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

        week52h = safe_float(info.get('fiftyTwoWeekHigh', hist['High'].max()))
        week52l = safe_float(info.get('fiftyTwoWeekLow',  hist['Low'].min()))

        bb_u = safe_float(hist['BB_upper'].iloc[-1])
        bb_m = safe_float(hist['BB_mid'].iloc[-1])
        bb_l = safe_float(hist['BB_lower'].iloc[-1])
        bb_width = round((bb_u - bb_l) / bb_m * 100, 2) if bb_m else 0
        bb_pos   = round((price - bb_l) / (bb_u - bb_l) * 100, 1) if (bb_u - bb_l) else 50

        # ── Quick financials from info (fast) ──
        short_ratio   = round(safe_float(info.get('shortRatio', 0)), 1)
        short_pct     = round(safe_float(info.get('shortPercentOfFloat', 0)) * 100, 2)
        profit_margin = round(safe_float(info.get('profitMargins', 0)) * 100, 1)
        roe           = round(safe_float(info.get('returnOnEquity', 0)) * 100, 1)
        gross_margin  = round(safe_float(info.get('grossMargins', 0)) * 100, 1)
        debt_equity   = round(safe_float(info.get('debtToEquity', 0)), 1)
        inst_pct      = round(safe_float(info.get('heldPercentInstitutions', 0)) * 100, 1)
        insider_pct   = round(safe_float(info.get('heldPercentInsiders', 0)) * 100, 1)
        rev_growth    = round(safe_float(info.get('revenueGrowth', 0)) * 100, 1)
        eps_growth    = round(safe_float(info.get('earningsGrowth', 0)) * 100, 1)
        fwd_eps       = round(safe_float(info.get('forwardEps', 0)), 2)

        levels      = get_levels(hist)
        conclusions = gen_conclusions(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v, vol_ratio)
        catalysts   = gen_catalysts(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v, vol_ratio, week52h, info)
        risks       = gen_risks(price, ma20, rsi_v, vol_ratio, week52h,
                                pe=safe_float(info.get('trailingPE',0)),
                                fwd_pe=safe_float(info.get('forwardPE',0)),
                                beta=safe_float(info.get('beta',1)),
                                debt_equity=safe_float(info.get('debtToEquity',0)),
                                sector=info.get('sector',''))
        strategy    = gen_strategy(price, ma5, ma20, ma60, rsi_v, levels,
                                    target_price=safe_float(info.get('targetMeanPrice', 0)))
        returns     = calc_returns(hist)
        invest_val  = gen_investment_value(
            price, ma5, ma20, ma60, macd_v, dea_v, rsi_v,
            pe=safe_float(info.get('trailingPE',0)),
            fwd_pe=safe_float(info.get('forwardPE',0)),
            roe=roe, profit_margin=profit_margin,
            rev_growth=rev_growth, eps_growth=eps_growth,
            beta=safe_float(info.get('beta',1)),
            debt_equity=debt_equity, vol_ratio=vol_ratio)

        # ── Quarterly revenue ──
        quarterly = []
        try:
            qf = stock.quarterly_financials
            if not qf.empty:
                for label in ['Total Revenue', 'Revenue']:
                    if label in qf.index:
                        row = qf.loc[label]
                        for col in row.index[:5]:
                            v = safe_float(row[col])
                            if v > 0:
                                quarterly.append({'period': str(col)[:7], 'revenue': round(v / 1e6, 1)})
                        break
        except:
            pass

        def clean(lst):
            res = []
            for x in lst:
                try:
                    f = float(x)
                    res.append(None if (np.isnan(f) or np.isinf(f)) else round(f, 4))
                except:
                    res.append(None)
            return res

        dates = hist.index.strftime('%Y-%m-%d').tolist()

        result = {
            'ticker':       ticker,
            'name':         info.get('longName', info.get('shortName', ticker)),
            'sector':       info.get('sector', ''),
            'industry':     info.get('industry', ''),
            'country':      info.get('country', ''),
            'description':  (info.get('longBusinessSummary', '') or '')[:300],
            'price':        round(price, 3),
            'change':       round(change, 3),
            'changePct':    round(change_pct, 2),
            'open':         round(safe_float(hist['Open'].iloc[-1]), 2),
            'high':         round(safe_float(hist['High'].iloc[-1]), 2),
            'low':          round(safe_float(hist['Low'].iloc[-1]), 2),
            'prevClose':    round(prev, 2),
            'volume':       safe_int(curr_vol),
            'avgVolume':    safe_int(avg_vol),
            'volRatio':     round(vol_ratio, 2),
            'marketCap':    safe_float(info.get('marketCap', 0)),
            'pe':           round(safe_float(info.get('trailingPE', 0)), 2),
            'forwardPe':    round(safe_float(info.get('forwardPE', 0)), 2),
            'eps':          round(safe_float(info.get('trailingEps', 0)), 2),
            'fwdEps':       fwd_eps,
            'beta':         round(safe_float(info.get('beta', 0)), 2),
            'divYield':     round(safe_div_yield_pct(info), 2),
            'sharesOut':    safe_int(info.get('sharesOutstanding', 0)),
            'week52High':   round(week52h, 2),
            'week52Low':    round(week52l, 2),
            'analystTarget': round(safe_float(info.get('targetMeanPrice', 0)), 2),
            'analystHigh':   round(safe_float(info.get('targetHighPrice', 0)), 2),
            'analystLow':    round(safe_float(info.get('targetLowPrice', 0)), 2),
            'recMean':       round(safe_float(info.get('recommendationMean', 3)), 2),
            'numAnalysts':   safe_int(info.get('numberOfAnalystOpinions', 0)),
            'shortRatio':   short_ratio,
            'shortPct':     short_pct,
            'profitMargin': profit_margin,
            'grossMargin':  gross_margin,
            'roe':          roe,
            'debtEquity':   debt_equity,
            'instPct':      inst_pct,
            'insiderPct':   insider_pct,
            'revGrowth':    rev_growth,
            'epsGrowth':    eps_growth,
            'ma5':    round(ma5, 2),
            'ma20':   round(ma20, 2),
            'ma60':   round(ma60, 2),
            'macdVal':  round(macd_v, 2),
            'deaVal':   round(dea_v, 2),
            'macdHist': round(macd_h, 2),
            'rsi':      round(rsi_v, 2),
            'bbUpper':  round(bb_u, 2),
            'bbMid':    round(bb_m, 2),
            'bbLower':  round(bb_l, 2),
            'bbWidth':  bb_width,
            'bbPos':    bb_pos,
            'levels':      levels,
            'conclusions': conclusions,
            'catalysts':   catalysts,
            'risks':       risks,
            'strategy':    strategy,
            'returns':     returns,
            'investValue': invest_val,
            'quarterly':   quarterly,
            'dates': dates,
            'ohlcv': {
                'open':   clean(hist['Open'].tolist()),
                'high':   clean(hist['High'].tolist()),
                'low':    clean(hist['Low'].tolist()),
                'close':  clean(hist['Close'].tolist()),
                'volume': [safe_int(x) for x in hist['Volume'].tolist()],
            },
            'ma': {
                'ma5':  clean(hist['MA5'].tolist()),
                'ma20': clean(hist['MA20'].tolist()),
                'ma60': clean(hist['MA60'].tolist()),
            },
            'macd': {
                'dif':  clean(hist['MACD'].tolist()),
                'dea':  clean(hist['Signal'].tolist()),
                'hist': clean(hist['MACDHist'].tolist()),
            },
            'bollinger': {
                'upper': clean(hist['BB_upper'].tolist()),
                'mid':   clean(hist['BB_mid'].tolist()),
                'lower': clean(hist['BB_lower'].tolist()),
            },
            'rsiSeries': clean(hist['RSI'].tolist()),
        }
        _cache_set(f'stock:{ticker}', result)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/news/<ticker>')
def get_news(ticker):
    ticker = ticker.upper().strip()
    cached = _cache_get(f'news:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock     = yf.Ticker(ticker)
        raw       = stock.news or []
        articles  = []
        for item in raw[:12]:
            c         = item.get('content', {})
            title     = c.get('title', '')
            publisher = (c.get('provider') or {}).get('displayName', '')
            url       = (c.get('canonicalUrl') or {}).get('url', '')
            summary   = c.get('summary', '') or ''
            pub_time  = c.get('pubDate', '')
            if title:
                articles.append({
                    'title':     title,
                    'publisher': publisher,
                    'url':       url,
                    'summary':   summary[:180],
                    'pubTime':   pub_time,
                })
        result = {'ticker': ticker, 'articles': articles}
        _cache_set(f'news:{ticker}', result, ttl=180)
        return jsonify(result)
    except Exception as e:
        return jsonify({'ticker': ticker, 'articles': [], 'error': str(e)})


# ── Taiwan Helpers ────────────────────────────────────────────────────
def tw_normalize(raw):
    raw = raw.strip().upper()
    if raw.endswith('.TW') or raw.endswith('.TWO'):
        return raw
    return raw + '.TW'

def tw_display(ticker):
    return ticker.replace('.TWO', '').replace('.TW', '')

def gen_tw_risks(price, ma20, rsi, vol_ratio, week52h,
                 pe=0, fwd_pe=0, beta=1.0, debt_equity=0, is_etf=False, inst_pct=0):
    risks = []
    from_high = (price - week52h) / week52h * 100 if week52h > 0 else 0
    ref_pe = fwd_pe if fwd_pe > 0 else pe

    if ref_pe > 30:
        risks.append({'level':'high',   'category':'估值風險', 'text':f'本益比 {ref_pe:.0f}x 高於台股歷史均值（約15-20x），業績須持續超預期才能支撐估值'})
    elif ref_pe > 20:
        risks.append({'level':'medium', 'category':'估值風險', 'text':f'本益比 {ref_pe:.0f}x 略高，需關注業績成長是否持續兌現'})

    if rsi > 75:
        risks.append({'level':'high',   'category':'技術風險', 'text':f'RSI {rsi:.0f} 嚴重超買，技術面過熱，短線回調風險高，建議等待拉回再布局'})
    elif rsi > 70:
        risks.append({'level':'medium', 'category':'技術風險', 'text':f'RSI {rsi:.0f} 進入超買區，短線追高需謹慎，可等待拉回均線再進場'})

    if from_high > -5:
        risks.append({'level':'medium', 'category':'技術風險', 'text':f'接近52週高點（距頂 {abs(from_high):.1f}%），面臨歷史強壓力區，突破需大量配合'})

    if price < ma20:
        risks.append({'level':'high',   'category':'趨勢風險', 'text':'跌破 MA20 均線，中期趨勢可能轉弱，建議降低部位等待均線翻多'})

    if beta > 1.5:
        risks.append({'level':'medium', 'category':'市場風險', 'text':f'Beta {beta:.1f}，波動性顯著高於大盤，市場修正時跌幅將放大'})

    if debt_equity > 150:
        risks.append({'level':'high',   'category':'財務風險', 'text':f'負債股東權益比 {debt_equity:.0f}%，財務槓桿偏高，利率上升或景氣下行壓力大'})
    elif debt_equity > 80:
        risks.append({'level':'medium', 'category':'財務風險', 'text':f'負債比 {debt_equity:.0f}%，需關注現金流與利息覆蓋能力'})

    if vol_ratio > 3.5:
        risks.append({'level':'medium', 'category':'籌碼風險', 'text':f'成交量爆量（{vol_ratio:.1f}x 均量），短期獲利了結賣壓可能增加，注意籌碼鬆動'})

    if is_etf:
        risks.append({'level':'low', 'category':'追蹤風險', 'text':'ETF 追蹤誤差與折溢價可能影響實際報酬，建議定期確認 NAV 與市價差異'})

    risks.append({'level':'medium', 'category':'地緣風險',
                  'text':'兩岸關係緊張及地緣政治局勢仍是台股最大不確定因素，可能引發外資快速撤離並衝擊市場'})
    risks.append({'level':'medium', 'category':'總經風險',
                  'text':'台灣央行利率政策、新台幣匯率走勢及全球景氣循環均對台股形成壓力，需密切追蹤'})
    if inst_pct >= 30:
        risks.append({'level':'low', 'category':'外資風險',
                      'text':f'外資持股約 {inst_pct:.0f}%，全球風險趨避情緒升溫時可能引發大量賣超衝擊流動性'})
    else:
        risks.append({'level':'low', 'category':'外資風險',
                      'text':'全球風險趨避情緒升溫時外資可能撤離台股，需追蹤外資進出籌碼動向'})
    return risks[:6]

def gen_tw_catalysts(price, ma5, ma20, ma60, macd, dea, rsi,
                     vol_ratio, week52h, info, is_etf=False):
    cats = []
    if price >= week52h * 0.97:
        cats.append({'num': 1, 'text': '突破或接近52週高點，強勢創高訊號',
                     'sub': '價格創新高，市場認可度提升，突破確認後動能強勁'})
    if macd > dea and macd > 0:
        cats.append({'num': len(cats)+1, 'text': 'MACD 金叉且在零軸上方，多頭動能強勁',
                     'sub': '短中期均偏多，技術面轉強訊號確立'})
    if vol_ratio >= 1.5:
        cats.append({'num': len(cats)+1, 'text': f'成交量放大（{vol_ratio:.1f}x 均量），買盤積極進場',
                     'sub': '成交量顯著高於均量，資金動能增強，籌碼活躍度提升'})
    if price > ma5 > ma20 > ma60:
        cats.append({'num': len(cats)+1, 'text': '均線多頭排列完整，趨勢強勢',
                     'sub': '短中長期均線支撐，回撐布局機會，趨勢延續性高'})

    if is_etf:
        div_yield    = safe_div_yield_pct(info)
        er           = safe_float(info.get('annualReportExpenseRatio', info.get('totalExpenseRatio', 0)))
        if er > 1: er /= 100
        er_pct       = round(er * 100, 2)
        etf_name     = (info.get('longName', '') or info.get('shortName', '') or '').lower()
        total_assets = safe_float(info.get('totalAssets', 0))
        is_leveraged = any(x in etf_name for x in ['正2', '2倍', 'leveraged', '2x'])
        is_inverse   = any(x in etf_name for x in ['反1', '放空', 'inverse', 'short'])

        extras = []

        # 費用率 — 顯示實際數字
        if er_pct > 0:
            extras.append({
                'text': f'費用率 {er_pct:.2f}%，持有成本低廉',
                'sub':  '管理費遠低於主動基金（通常 1–2%），長期持有複利優勢顯著'
            })
        else:
            extras.append({
                'text': '費用率低廉，長期複利效果顯著優越',
                'sub':  '相較主動基金費用低，長期績效差異大'
            })

        # 配息 vs 不配息 — 依實際殖利率顯示
        if div_yield >= 4.0:
            extras.append({
                'text': f'年化殖利率 {div_yield:.1f}%，現金流收益豐厚',
                'sub':  '定期配息提供穩定現金流，適合退休規劃與存股族'
            })
        elif div_yield > 0:
            extras.append({
                'text': f'殖利率 {div_yield:.1f}%，兼顧配息與資本利得',
                'sub':  '配息搭配指數追蹤，平衡現金收益與長期成長'
            })
        else:
            extras.append({
                'text': '不配息累積型，獲利全額自動再投入',
                'sub':  '無配息扣稅損耗，資本利得完整保留並持續複利增值'
            })

        # 策略特色 — 槓桿 / 反向 / 一般指數
        if is_leveraged:
            extras.append({
                'text': '兩倍槓桿放大報酬，多頭行情效益顯著',
                'sub':  '追蹤指數每日報酬的兩倍，趨勢向上時效益倍增，適合短線操作'
            })
        elif is_inverse:
            extras.append({
                'text': '反向操作工具，空頭市場避險利器',
                'sub':  '指數下跌時獲利，適合對沖部位或空頭趨勢交易'
            })
        else:
            extras.append({
                'text': '指數化投資，分散個股風險，定期定額首選',
                'sub':  '追蹤指數自動汰弱留強，分散集中持股風險，適合穩健長期投資人'
            })

        # 規模流動性 — 依 AUM 顯示
        if total_assets >= 1e10:
            extras.append({
                'text': f'基金規模 {total_assets/1e8:.0f} 億，流動性充裕',
                'sub':  '龐大資產規模確保市場深度，買賣價差小，追蹤誤差低'
            })
        else:
            extras.append({
                'text': '交易所掛牌，流動性佳買賣靈活',
                'sub':  '隨時可在市場交易，不受申購贖回限制，彈性高於一般基金'
            })
    else:
        div_yield  = safe_div_yield_pct(info)
        rev_growth = safe_float(info.get('revenueGrowth',  0)) * 100
        eps_growth = safe_float(info.get('earningsGrowth', 0)) * 100
        roe        = safe_float(info.get('returnOnEquity', 0)) * 100
        inst_pct   = safe_float(info.get('heldPercentInstitutions', 0)) * 100
        pm         = safe_float(info.get('profitMargins', 0)) * 100
        sector     = (info.get('sector', '') or '').lower()

        extras = []

        # 產業特色
        if any(x in sector for x in ['technology', 'semiconductor', 'electronic']):
            extras.append({'text': 'AI 與半導體需求旺盛，科技產業持續受惠',
                           'sub': '全球 AI 基礎建設擴張帶動台廠訂單能見度提升，龍頭廠商議價能力強'})
        elif 'financial' in sector:
            extras.append({'text': '金融股配息穩健，利差擴大支撐獲利',
                           'sub': '升息環境擴大淨利差，放款成長帶動手續費收入，現金流穩定'})
        elif 'consumer' in sector:
            extras.append({'text': '內需消費穩健，現金流充裕抗景氣循環',
                           'sub': '台灣消費市場穩定，剛性需求支撐營收，獲利波動低'})
        elif 'health' in sector:
            extras.append({'text': '醫療產業受惠高齡化趨勢，長期需求穩定',
                           'sub': '人口老齡化驅動醫療支出持續增長，政策支持力道強勁'})
        elif any(x in sector for x in ['energy', 'utilities', 'material']):
            extras.append({'text': '原物料與能源需求回升，景氣敏感度高',
                           'sub': '全球基礎建設投資帶動需求，景氣復甦期間彈性大'})
        elif 'industrial' in sector:
            extras.append({'text': '工業製造供應鏈完整，接單能見度佳',
                           'sub': '台灣製造業競爭力強，全球供應鏈重組帶來轉單效應'})
        else:
            extras.append({'text': '台股優質企業，產業地位穩固具護城河',
                           'sub': '市場份額領先，長期競爭優勢明確，獲利能力具持續性'})

        # 成長動能
        if rev_growth >= 20:
            extras.append({'text': f'營收年增 {rev_growth:.0f}%，成長動能強勁',
                           'sub': '高速成長驗證市場需求，法人持續追捧成長型標的，估值重估空間大'})
        elif eps_growth >= 20:
            extras.append({'text': f'EPS 年增 {eps_growth:.0f}%，獲利加速擴張',
                           'sub': '獲利成長超預期，帶動本益比重估上修，成長邏輯持續兌現'})
        elif roe >= 20:
            extras.append({'text': f'ROE {roe:.0f}%，資本配置效率優異',
                           'sub': '高股東報酬率顯示管理層創值能力強，具長期複利投資價值'})
        elif pm >= 15:
            extras.append({'text': f'淨利率 {pm:.0f}%，獲利品質優異',
                           'sub': '高利潤率反映定價能力與成本控制到位，護城河深厚'})
        else:
            extras.append({'text': '技術面蓄積整理，突破動能持續累積',
                           'sub': '量縮整理後靜待放量突破，籌碼沉澱後上漲空間可期'})

        # 配息 / 成長
        if div_yield >= 5.0:
            extras.append({'text': f'殖利率 {div_yield:.1f}%，高息存股首選',
                           'sub': '高殖利率具防禦優勢，穩定股息保護下檔，吸引長期存股族'})
        elif div_yield >= 2.5:
            extras.append({'text': f'殖利率 {div_yield:.1f}%，配息具吸引力',
                           'sub': '穩定配息反映現金流健康，兼顧股息收益與資本利得'})
        elif div_yield > 0:
            extras.append({'text': f'殖利率 {div_yield:.1f}%，維持配息政策',
                           'sub': '公司具配息能力，保留盈餘同時維持股東回饋'})
        else:
            extras.append({'text': '成長型個股，獲利持續再投入擴張',
                           'sub': '保留盈餘用於業務擴張與研發投入，聚焦長期資本增值'})

        # 法人籌碼
        if inst_pct >= 50:
            extras.append({'text': f'法人持股 {inst_pct:.0f}%，籌碼集中穩固',
                           'sub': '高法人持股比例代表機構長期看好，籌碼穩定不易恐慌性賣壓'})
        elif inst_pct >= 20:
            extras.append({'text': f'法人持股 {inst_pct:.0f}%，機構認同度佳',
                           'sub': '外資與投信積極布局，籌碼結構改善，主力護盤意願強'})
        else:
            extras.append({'text': '三大法人動向值得追蹤，籌碼面待觀察',
                           'sub': '法人進出往往領先散戶，追蹤外資投信動向可掌握主力意圖'})

    if is_etf:
        # ETF：技術訊號最多保留 2 條，其餘必須顯示 ETF 特有資訊（費用率/配息/策略/規模）
        cats = cats[:2]
        for ex in extras:
            if len(cats) >= 4:
                break
            cats.append({'num': len(cats)+1, **ex})
    else:
        while len(cats) < 4:
            cats.append({'num': len(cats)+1, **extras[len(cats) % len(extras)]})
    return cats[:4]


# ── Taiwan Routes ─────────────────────────────────────────────────────
@app.route('/tw')
def tw_index():
    return render_template('tw_stock.html')


@app.route('/api/tw/market')
def get_tw_market():
    cached = _cache_get('tw_market')
    if cached: return jsonify(cached)
    syms = {
        'twii':   '^TWII',
        'twoii':  '^TWOII',
        'usdtwd': 'USDTWD=X',
        'gold':   'GC=F',
        'vix':    '^VIX',
    }
    result = {}
    for key, sym in syms.items():
        try:
            h = yf.Ticker(sym).history(period='2d')
            if len(h) >= 2:
                cur  = safe_float(h['Close'].iloc[-1])
                prev = safe_float(h['Close'].iloc[-2])
                pct  = (cur / prev - 1) * 100 if prev else 0
                result[key] = {'v': round(cur, 2), 'pct': round(pct, 2)}
            elif len(h) == 1:
                result[key] = {'v': round(safe_float(h['Close'].iloc[-1]), 2), 'pct': 0}
            else:
                result[key] = None
        except:
            result[key] = None

    vix_val = (result.get('vix') or {}).get('v', 20)
    if   vix_val < 15: label, cls = '極度貪婪', 'greed-hi'
    elif vix_val < 20: label, cls = '貪婪',     'greed'
    elif vix_val < 25: label, cls = '中性',     'neutral-m'
    elif vix_val < 30: label, cls = '恐懼',     'fear'
    else:              label, cls = '極度恐懼', 'fear-hi'
    result['vixLabel'] = label
    result['vixCls']   = cls
    _cache_set('tw_market', result, ttl=60)
    return jsonify(result)


@app.route('/api/tw/stock/<ticker>')
def get_tw_stock(ticker):
    ticker = tw_normalize(ticker)
    cached = _cache_get(f'tw_stock:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        hist  = stock.history(period='1y')

        # Fallback .TWO
        if hist.empty and ticker.endswith('.TW'):
            alt   = ticker.replace('.TW', '.TWO')
            stock = yf.Ticker(alt)
            info  = stock.info
            hist  = stock.history(period='1y')
            if not hist.empty:
                ticker = alt

        if hist.empty:
            return jsonify({'error': f'找不到股票 {ticker}，請確認代碼是否正確'}), 404

        is_etf = info.get('quoteType', '').upper() == 'ETF'

        hist['MA5']  = hist['Close'].rolling(5).mean()
        hist['MA20'] = hist['Close'].rolling(20).mean()
        hist['MA60'] = hist['Close'].rolling(60).mean()
        macd_s, sig_s, hist_s = calc_macd(hist['Close'])
        hist['MACD']     = macd_s
        hist['Signal']   = sig_s
        hist['MACDHist'] = hist_s
        hist['RSI']      = calc_rsi(hist['Close'])
        bb_u, bb_m, bb_l = calc_bollinger(hist['Close'])
        hist['BB_upper'] = bb_u
        hist['BB_mid']   = bb_m
        hist['BB_lower'] = bb_l

        price = safe_float(hist['Close'].iloc[-1])
        prev  = safe_float(hist['Close'].iloc[-2])
        change     = price - prev
        change_pct = change / prev * 100 if prev else 0

        ma5    = safe_float(hist['MA5'].iloc[-1])
        ma20   = safe_float(hist['MA20'].iloc[-1])
        ma60   = safe_float(hist['MA60'].iloc[-1])
        macd_v = safe_float(hist['MACD'].iloc[-1])
        dea_v  = safe_float(hist['Signal'].iloc[-1])
        macd_h = safe_float(hist['MACDHist'].iloc[-1])
        rsi_v  = safe_float(hist['RSI'].iloc[-1])

        avg_vol  = safe_float(hist['Volume'].rolling(20).mean().iloc[-1])
        curr_vol = safe_float(hist['Volume'].iloc[-1])
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

        week52h = safe_float(info.get('fiftyTwoWeekHigh', hist['High'].max()))
        week52l = safe_float(info.get('fiftyTwoWeekLow',  hist['Low'].min()))

        bbu = safe_float(hist['BB_upper'].iloc[-1])
        bbm = safe_float(hist['BB_mid'].iloc[-1])
        bbl = safe_float(hist['BB_lower'].iloc[-1])
        bb_width = round((bbu - bbl) / bbm * 100, 2) if bbm else 0
        bb_pos   = round((price - bbl) / (bbu - bbl) * 100, 1) if (bbu - bbl) else 50

        profit_margin = round(safe_float(info.get('profitMargins',     0)) * 100, 1)
        roe           = round(safe_float(info.get('returnOnEquity',     0)) * 100, 1)
        gross_margin  = round(safe_float(info.get('grossMargins',       0)) * 100, 1)
        debt_equity   = round(safe_float(info.get('debtToEquity',       0)), 1)
        inst_pct      = round(safe_float(info.get('heldPercentInstitutions', 0)) * 100, 1)
        insider_pct   = round(safe_float(info.get('heldPercentInsiders', 0)) * 100, 1)
        rev_growth    = round(safe_float(info.get('revenueGrowth',      0)) * 100, 1)
        eps_growth    = round(safe_float(info.get('earningsGrowth',     0)) * 100, 1)
        short_ratio   = round(safe_float(info.get('shortRatio',         0)), 1)
        short_pct     = round(safe_float(info.get('shortPercentOfFloat',0)) * 100, 2)
        fwd_eps       = round(safe_float(info.get('forwardEps',         0)), 2)

        levels      = get_levels(hist)
        conclusions = gen_conclusions(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v, vol_ratio)
        catalysts   = gen_tw_catalysts(price, ma5, ma20, ma60, macd_v, dea_v, rsi_v,
                                       vol_ratio, week52h, info, is_etf)
        risks       = gen_tw_risks(price, ma20, rsi_v, vol_ratio, week52h,
                                   pe=safe_float(info.get('trailingPE', 0)),
                                   fwd_pe=safe_float(info.get('forwardPE', 0)),
                                   beta=safe_float(info.get('beta', 1)),
                                   debt_equity=safe_float(info.get('debtToEquity', 0)),
                                   is_etf=is_etf,
                                   inst_pct=safe_float(info.get('heldPercentInstitutions', 0)) * 100)
        strategy    = gen_tw_strategy(price, ma5, ma20, ma60, rsi_v, levels,
                                      week52h, week52l, info)
        returns     = calc_returns(hist)
        if is_etf:
            invest_val = gen_etf_invest_value(
                price, ma5, ma20, ma60, macd_v, dea_v, rsi_v, vol_ratio, info)
        else:
            invest_val = gen_investment_value(
                price, ma5, ma20, ma60, macd_v, dea_v, rsi_v,
                pe=safe_float(info.get('trailingPE', 0)),
                fwd_pe=safe_float(info.get('forwardPE', 0)),
                roe=roe, profit_margin=profit_margin,
                rev_growth=rev_growth, eps_growth=eps_growth,
                beta=safe_float(info.get('beta', 1)),
                debt_equity=debt_equity, vol_ratio=vol_ratio)

        quarterly = []
        try:
            qf = stock.quarterly_financials
            if not qf.empty:
                for lbl in ['Total Revenue', 'Revenue']:
                    if lbl in qf.index:
                        row = qf.loc[lbl]
                        for col in row.index[:5]:
                            v = safe_float(row[col])
                            if v > 0:
                                quarterly.append({'period': str(col)[:7], 'revenue': round(v / 1e6, 1)})
                        break
        except:
            pass

        # ETF extra data
        etf_data = None
        if is_etf:
            ta  = safe_float(info.get('totalAssets', 0))
            er  = safe_float(info.get('annualReportExpenseRatio', info.get('totalExpenseRatio', 0)))
            if er > 1: er /= 100
            nav = safe_float(info.get('navPrice', 0))
            prem = round((price - nav) / nav * 100, 2) if nav > 0 else 0
            etf_data = {
                'totalAssets':   round(ta / 1e8, 1),
                'expenseRatio':  round(er * 100, 4) if er > 0 else 0,
                'threeYrReturn': round(safe_float(info.get('threeYearAverageReturn', 0)) * 100, 2),
                'fiveYrReturn':  round(safe_float(info.get('fiveYearAverageReturn',  0)) * 100, 2),
                'ytdReturn':     round(safe_float(info.get('ytdReturn', 0)) * 100, 2),
                'category':      info.get('category', ''),
                'fundFamily':    info.get('fundFamily', ''),
                'nav':           nav,
                'premium':       prem,
            }

        def clean(lst):
            res = []
            for x in lst:
                try:
                    f = float(x)
                    res.append(None if (np.isnan(f) or np.isinf(f)) else round(f, 4))
                except:
                    res.append(None)
            return res

        dates = hist.index.strftime('%Y-%m-%d').tolist()
        result = {
            'ticker':        ticker,
            'displayTicker': tw_display(ticker),
            'name':          info.get('longName', info.get('shortName', ticker)),
            'sector':        info.get('sector', ''),
            'industry':      info.get('industry', ''),
            'country':       info.get('country', 'Taiwan'),
            'description':   (info.get('longBusinessSummary', '') or '')[:300],
            'price':         round(price, 2),
            'change':        round(change, 2),
            'changePct':     round(change_pct, 2),
            'open':          round(safe_float(hist['Open'].iloc[-1]), 2),
            'high':          round(safe_float(hist['High'].iloc[-1]), 2),
            'low':           round(safe_float(hist['Low'].iloc[-1]), 2),
            'prevClose':     round(prev, 2),
            'volume':        safe_int(curr_vol),
            'avgVolume':     safe_int(avg_vol),
            'volRatio':      round(vol_ratio, 2),
            'marketCap':     safe_float(info.get('marketCap', 0)),
            'pe':            round(safe_float(info.get('trailingPE',  0)), 2),
            'forwardPe':     round(safe_float(info.get('forwardPE',   0)), 2),
            'eps':           round(safe_float(info.get('trailingEps', 0)), 2),
            'fwdEps':        fwd_eps,
            'beta':          round(safe_float(info.get('beta',        0)), 2),
            'divYield':      round(safe_div_yield_pct(info), 2),
            'sharesOut':     safe_int(info.get('sharesOutstanding', 0)),
            'week52High':    round(week52h, 2),
            'week52Low':     round(week52l, 2),
            'analystTarget': round(safe_float(info.get('targetMeanPrice', 0)), 2),
            'analystHigh':   round(safe_float(info.get('targetHighPrice',  0)), 2),
            'analystLow':    round(safe_float(info.get('targetLowPrice',   0)), 2),
            'recMean':       round(safe_float(info.get('recommendationMean', 3)), 2),
            'numAnalysts':   safe_int(info.get('numberOfAnalystOpinions', 0)),
            'shortRatio':    short_ratio, 'shortPct':    short_pct,
            'profitMargin':  profit_margin, 'grossMargin': gross_margin,
            'roe':           roe, 'debtEquity': debt_equity,
            'instPct':       inst_pct, 'insiderPct': insider_pct,
            'revGrowth':     rev_growth, 'epsGrowth':  eps_growth,
            'ma5':     round(ma5, 2),  'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
            'macdVal': round(macd_v, 2), 'deaVal': round(dea_v, 2), 'macdHist': round(macd_h, 2),
            'rsi':     round(rsi_v, 2),
            'bbUpper': round(bbu, 2), 'bbMid': round(bbm, 2), 'bbLower': round(bbl, 2),
            'bbWidth': bb_width, 'bbPos': bb_pos,
            'levels': levels, 'conclusions': conclusions, 'catalysts': catalysts,
            'risks': risks, 'strategy': strategy, 'returns': returns,
            'investValue': invest_val, 'quarterly': quarterly,
            'isEtf': is_etf, 'etfData': etf_data,
            'dates': dates,
            'ohlcv': {
                'open':   clean(hist['Open'].tolist()),
                'high':   clean(hist['High'].tolist()),
                'low':    clean(hist['Low'].tolist()),
                'close':  clean(hist['Close'].tolist()),
                'volume': [safe_int(x) for x in hist['Volume'].tolist()],
            },
            'ma':        {'ma5': clean(hist['MA5'].tolist()), 'ma20': clean(hist['MA20'].tolist()), 'ma60': clean(hist['MA60'].tolist())},
            'macd':      {'dif': clean(hist['MACD'].tolist()), 'dea': clean(hist['Signal'].tolist()), 'hist': clean(hist['MACDHist'].tolist())},
            'bollinger': {'upper': clean(hist['BB_upper'].tolist()), 'mid': clean(hist['BB_mid'].tolist()), 'lower': clean(hist['BB_lower'].tolist())},
            'rsiSeries': clean(hist['RSI'].tolist()),
        }
        _cache_set(f'tw_stock:{ticker}', result)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _fetch_gnews(query, max_results=10):
    try:
        q   = urllib.parse.quote(query)
        url = f'https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
        r   = _requests.get(url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return []
        root  = ET.fromstring(r.content)
        items = root.findall('.//item')
        out   = []
        for item in items[:max_results]:
            title = item.findtext('title', '').strip()
            link  = item.findtext('link', '').strip()
            pub   = item.findtext('pubDate', '')
            src   = item.find('source')
            publisher = src.text.strip() if src is not None else 'Google News'
            if title and link:
                out.append({'title': title, 'publisher': publisher, 'url': link,
                            'summary': '', 'pubTime': pub})
        return out
    except Exception:
        return []


@app.route('/api/tw/intraday/<ticker>')
def get_tw_intraday(ticker):
    ticker = tw_normalize(ticker)
    cached = _cache_get(f'tw_intra:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(period='1d', interval='5m')

        # Fallback .TWO
        if hist.empty and ticker.endswith('.TW'):
            alt   = ticker.replace('.TW', '.TWO')
            hist  = yf.Ticker(alt).history(period='1d', interval='5m')

        if hist.empty:
            return jsonify({'error': '暫無當日分鐘資料'}), 404

        # Keep only the latest trading date
        last_date = hist.index.date[-1]
        hist = hist[hist.index.date == last_date]

        # Format times as HH:MM (Asia/Taipei)
        times  = hist.index.tz_convert('Asia/Taipei').strftime('%H:%M').tolist()

        def clean_list(lst):
            res = []
            for x in lst:
                try:
                    f = float(x)
                    res.append(None if (np.isnan(f) or np.isinf(f) or f == 0) else round(f, 4))
                except:
                    res.append(None)
            return res

        closes  = clean_list(hist['Close'].tolist())
        opens   = clean_list(hist['Open'].tolist())
        highs   = clean_list(hist['High'].tolist())
        lows    = clean_list(hist['Low'].tolist())
        volumes = [safe_int(x) for x in hist['Volume'].tolist()]

        result = {
            'ticker': ticker,
            'date':   str(last_date),
            'times':  times,
            'ohlcv':  {'open': opens, 'high': highs, 'low': lows, 'close': closes, 'volume': volumes},
        }
        _cache_set(f'tw_intra:{ticker}', result, ttl=60)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _infer_etf_methodology(name, sectors, holdings):
    n = (name or '').lower()
    if 'top 50' in n or '台灣50' in n:
        return '追蹤「臺灣50指數」，從上市公司中選出市值最大前50家，採市值加權，為台股藍籌股代表性指標。'
    if 'mid-cap' in n or 'midcap' in n or '中型' in n:
        return '追蹤台股中型股指數，補足大型股以外的中市值企業曝險，市值介於大型股與小型股之間。'
    if 'nasdaq' in n:
        return '追蹤 NASDAQ-100 指數，涵蓋美國那斯達克交易所市值最大100家非金融企業，科技比重高達50%以上。'
    if 'sp500' in n or 's&p 500' in n or 'sp 500' in n:
        return '追蹤 S&P 500 指數，涵蓋美國500大市值企業，分散投資於全市場，為全球最具代表性的股市指標。'
    if 'semiconductor' in n or '半導體' in n:
        return '聚焦半導體產業鏈（IC設計、晶圓代工、封測等），隨AI需求與科技週期波動，適合積極型投資人。'
    if 'high dividend' in n or '高息' in n or '高股息' in n or 'dividend' in n:
        return '以高殖利率為主要選股邏輯，篩選配息穩定且殖利率高於市場平均個股，重視現金流的收益型投資人首選。'
    if 'esg' in n:
        return '依 ESG（環境、社會、公司治理）標準篩選，排除高碳排或治理不佳企業，兼顧長期報酬與永續理念。'
    if '正2' in n or 'leveraged' in n or '2x' in n:
        return '正向2倍槓桿ETF，每日追蹤標的指數2倍報酬，適合短線波段，長期持有有複利衰減風險，非長線工具。'
    if '反1' in n or '空' in n or 'inverse' in n or 'bear' in n:
        return '反向1倍ETF，追蹤標的指數的負1倍日報酬，可作空頭避險工具，不適合長期持有。'
    if 'reit' in n or 'real estate' in n:
        return '投資不動產投資信託（REITs），透過持有商業不動產或抵押貸款提供穩定租金收益，配息頻率通常較高。'
    if 'bond' in n or 'government' in n or '公債' in n or '債' in n:
        return '追蹤固定收益（債券）指數，以政府或公司債為主要持倉，低波動、穩定息收，可作投資組合防禦配置。'
    # Infer from top sector
    if sectors:
        top_sec = max(sectors, key=sectors.get)
        sec_names = {
            'technology': '科技產業', 'financial_services': '金融業',
            'healthcare': '醫療保健', 'consumer_cyclical': '消費類',
            'industrials': '工業', 'basic_materials': '原物料',
            'communication_services': '通訊服務', 'energy': '能源',
        }
        s = sec_names.get(top_sec, top_sec)
        return f'採指數化被動管理策略，{s}產業權重最高，追蹤特定指數以分散個股集中風險、降低管理費用。'
    return '採指數化被動管理策略，追蹤特定基準指數，以分散投資降低個股集中風險。'


def _gen_etf_entry(price, nav, rsi, ma20, ma60, div_yield, is_lev):
    score = 0
    signals = []
    if nav > 0:
        prem = round((price - nav) / nav * 100, 2)
        if prem < -2:   score += 2; signals.append(('buy',  f'折價 {abs(prem):.1f}%，低於淨值具吸引力'))
        elif prem > 3:  score -= 1; signals.append(('warn', f'溢價 {prem:.1f}%，高於淨值謹慎追價'))
        else:           score += 1; signals.append(('ok',   f'溢/折價 {prem:.1f}%，接近淨值合理'))
    if price > ma20 and ma20 > 0 and ma60 > 0 and ma20 > ma60:
        score += 1; signals.append(('buy',  '站上 MA20/60，中長期趨勢偏多'))
    elif ma60 > 0 and price < ma60:
        score -= 1; signals.append(('warn', '跌破 MA60，中期趨勢偏弱'))
    if rsi > 0:
        if rsi < 35:    score += 2; signals.append(('buy',  f'RSI {rsi:.0f} 超賣，逢低布局機會'))
        elif rsi > 72:  score -= 1; signals.append(('warn', f'RSI {rsi:.0f} 超買，短線謹慎'))
        elif 45 <= rsi <= 65: score += 1; signals.append(('ok', f'RSI {rsi:.0f} 健康動能區間'))
    if div_yield > 4:   score += 1; signals.append(('buy',  f'殖利率 {div_yield:.1f}%，息收具吸引力'))
    elif div_yield > 2: signals.append(('ok',   f'殖利率 {div_yield:.1f}%，一般水準'))
    if is_lev:          signals.append(('warn', '槓桿/反向ETF，僅適合短線波段，不宜長期持有'))
    if score >= 4:   rec, col = '強力建議進場',    '#00d68f'
    elif score >= 2: rec, col = '可分批布局',       '#3d8ef8'
    elif score >= 0: rec, col = '觀望等待時機',     '#f0b429'
    else:            rec, col = '暫不建議，等待回調', '#e84646'
    return {'score': score, 'rec': rec, 'color': col, 'signals': signals}


@app.route('/api/tw/etf_detail/<ticker>')
def get_tw_etf_detail(ticker):
    ticker = tw_normalize(ticker)
    cached = _cache_get(f'tw_etf_detail:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        # Dividends
        div_history = []
        div_freq_label = 'N/A'
        next_div_est   = None
        try:
            divs = stock.dividends
            if not divs.empty:
                divs_sorted = divs.sort_index()
                div_history = [{'date': d.strftime('%Y-%m-%d'), 'amount': round(float(v), 4)}
                               for d, v in divs_sorted.items()]
                if len(div_history) >= 2:
                    from datetime import datetime as _dt, timedelta as _td
                    dates = [_dt.strptime(x['date'], '%Y-%m-%d') for x in div_history]
                    gaps  = [(dates[i+1]-dates[i]).days for i in range(len(dates)-1)]
                    avg   = sum(gaps[-6:]) / min(6, len(gaps))
                    if avg <= 105:   div_freq_label = '季配'
                    elif avg <= 200: div_freq_label = '半年配'
                    else:            div_freq_label = '年配'
                    nxt = dates[-1] + _td(days=int(avg))
                    next_div_est = nxt.strftime('%Y-%m')
        except Exception:
            pass

        # Holdings
        holdings = []
        sectors  = {}
        asset_classes = {}
        try:
            fd = stock.funds_data
            if fd is not None:
                th = fd.top_holdings
                if th is not None and not th.empty:
                    for sym, row in th.iterrows():
                        holdings.append({
                            'symbol': str(sym).replace('.TW','').replace('.TWO',''),
                            'name':   str(row.get('Name', sym))[:25],
                            'pct':    round(float(row.get('Holding Percent', 0)) * 100, 2)
                        })
                sw = fd.sector_weightings
                if sw is not None:
                    for k, v in (sw.items() if isinstance(sw, dict) else sw.to_dict().items()):
                        if float(v) > 0.001:
                            sectors[k] = round(float(v) * 100, 2)
                ac = fd.asset_classes
                if ac is not None:
                    for k, v in (ac.items() if isinstance(ac, dict) else ac.to_dict().items()):
                        if float(v) > 0.001:
                            asset_classes[k] = round(float(v) * 100, 2)
        except Exception:
            pass

        nav   = safe_float(info.get('navPrice', 0))
        price = safe_float(info.get('regularMarketPrice', info.get('previousClose', 0)))
        prem  = round((price - nav) / nav * 100, 2) if nav > 0 else 0
        er    = safe_float(info.get('annualReportExpenseRatio', info.get('totalExpenseRatio', 0)))
        if er > 1: er /= 100
        ta    = safe_float(info.get('totalAssets', 0))
        dy    = round(safe_div_yield_pct(info), 2)
        rsi   = safe_float(info.get('twoHundredDayAverage', 0))  # placeholder; main route has real RSI
        name  = info.get('longName', info.get('shortName', ticker))
        is_lev = any(x in (name or '').lower() for x in ['正2','leveraged','2x','inverse','反1','bear'])
        inception = info.get('fundInceptionDate', 0)
        from datetime import datetime as _dt
        inception_str = _dt.utcfromtimestamp(inception).strftime('%Y-%m-%d') if inception else ''

        methodology = _infer_etf_methodology(name, sectors, holdings)

        result = {
            'ticker': ticker,
            'nav': nav,
            'premium': prem,
            'totalAssets': round(ta / 1e8, 1),
            'expenseRatio': round(er * 100, 4) if er > 0 else 0,
            'divYield': dy,
            'divFreq': div_freq_label,
            'nextDivEst': next_div_est,
            'divHistory': div_history[-12:],
            'holdings': holdings[:10],
            'sectors': sectors,
            'assetClasses': asset_classes,
            'methodology': methodology,
            'inceptionDate': inception_str,
            'isLeveraged': is_lev,
        }
        _cache_set(f'tw_etf_detail:{ticker}', result, ttl=3600)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/tw/news/<ticker>')
def get_tw_news(ticker):
    ticker = tw_normalize(ticker)
    cached = _cache_get(f'tw_news:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock    = yf.Ticker(ticker)
        info     = stock.info
        raw_news = stock.news or []
        articles = []
        seen_titles = set()
        for item in raw_news[:12]:
            c         = item.get('content', {})
            title     = c.get('title', '')
            publisher = (c.get('provider') or {}).get('displayName', '')
            url       = (c.get('canonicalUrl') or {}).get('url', '')
            summary   = c.get('summary', '') or ''
            pub_time  = c.get('pubDate', '')
            if title and title not in seen_titles:
                seen_titles.add(title)
                articles.append({'title': title, 'publisher': publisher, 'url': url,
                                  'summary': summary[:180], 'pubTime': pub_time})

        # Supplement with Google News if fewer than 6 articles
        if len(articles) < 6:
            code = ticker.replace('.TW','').replace('.TWO','')
            query = f'{code} 台股'
            gn = _fetch_gnews(query, max_results=12)
            for a in gn:
                if a['title'] not in seen_titles:
                    seen_titles.add(a['title'])
                    articles.append(a)
                    if len(articles) >= 15:
                        break

        result = {'ticker': ticker, 'articles': articles}
        _cache_set(f'tw_news:{ticker}', result, ttl=180)
        return jsonify(result)
    except Exception as e:
        return jsonify({'ticker': ticker, 'articles': [], 'error': str(e)})


@app.route('/api/tw/fundamentals/<ticker>')
def get_tw_fundamentals(ticker):
    ticker = tw_normalize(ticker)
    cached = _cache_get(f'tw_fund:{ticker}')
    if cached: return jsonify(cached)
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        ocf_val = fcf_val = 0
        try:
            cf = stock.cashflow
            if cf is not None and not cf.empty:
                for lbl in ['Operating Cash Flow', 'Total Cash From Operating Activities']:
                    if lbl in cf.index:
                        ocf_val = safe_float(cf.loc[lbl].iloc[0]); break
                for lbl in ['Free Cash Flow']:
                    if lbl in cf.index:
                        fcf_val = safe_float(cf.loc[lbl].iloc[0]); break
                if fcf_val == 0 and ocf_val != 0:
                    for lbl in ['Capital Expenditure', 'Capital Expenditures']:
                        if lbl in cf.index:
                            fcf_val = ocf_val + safe_float(cf.loc[lbl].iloc[0]); break
        except:
            pass

        top_holders = []
        try:
            ih = stock.institutional_holders
            if ih is not None and not ih.empty:
                cols = [str(c) for c in ih.columns]
                name_col = next((c for c in cols if 'holder' in c.lower() or 'institution' in c.lower()), None)
                pct_col  = next((c for c in cols if 'pct' in c.lower() or '%' in c or 'out' in c.lower()), None)
                val_col  = next((c for c in cols if 'value' in c.lower()), None)
                if name_col:
                    for _, row in ih.head(5).iterrows():
                        holder = str(row[name_col])
                        if holder and holder != 'nan' and not holder[:4].isdigit():
                            pct = safe_float(row[pct_col]) if pct_col else 0
                            val = safe_float(row[val_col]) if val_col else 0
                            pct_disp = round(pct * 100, 2) if pct < 1 else round(pct, 2)
                            top_holders.append({'holder': holder[:35], 'pct': pct_disp,
                                                'value': round(val / 1e9, 2)})
        except:
            pass

        earnings_date = None
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                col = cal.columns[0]
                earnings_date = str(col.date()) if hasattr(col, 'date') else str(col)[:10]
        except:
            pass

        mktcap    = safe_float(info.get('marketCap', 0))
        fcf_yield = round(fcf_val / mktcap * 100, 2) if mktcap and fcf_val else 0
        result = {
            'ticker':       ticker,
            'ocf':          round(ocf_val / 1e8, 2),
            'fcf':          round(fcf_val / 1e8, 2),
            'fcfYield':     fcf_yield,
            'pfcf':         round(mktcap / fcf_val, 1) if fcf_val and fcf_val > 0 else None,
            'debtEquity':   round(safe_float(info.get('debtToEquity',       0)), 1),
            'currentRatio': round(safe_float(info.get('currentRatio',        0)), 2),
            'roe':          round(safe_float(info.get('returnOnEquity',      0)) * 100, 1),
            'roa':          round(safe_float(info.get('returnOnAssets',      0)) * 100, 1),
            'profitMargin': round(safe_float(info.get('profitMargins',       0)) * 100, 1),
            'grossMargin':  round(safe_float(info.get('grossMargins',        0)) * 100, 1),
            'instPct':      round(safe_float(info.get('heldPercentInstitutions', 0)) * 100, 1),
            'insiderPct':   round(safe_float(info.get('heldPercentInsiders', 0)) * 100, 1),
            'shortRatio':   round(safe_float(info.get('shortRatio',          0)), 1),
            'shortPct':     round(safe_float(info.get('shortPercentOfFloat', 0)) * 100, 2),
            'earningsDate': earnings_date,
            'epsEst':       round(safe_float(info.get('forwardEps',          0)), 2),
            'revGrowth':    round(safe_float(info.get('revenueGrowth',       0)) * 100, 1),
            'epsGrowth':    round(safe_float(info.get('earningsGrowth',      0)) * 100, 1),
            'topHolders':   top_holders,
        }
        _cache_set(f'tw_fund:{ticker}', result)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Broker Chips Helpers ───────────────────────────────────────────────
_BROKER_TAGS = {
    '美林': '外資', '摩根大通': '外資', '摩根士丹利': '外資',
    '高盛': '外資', '瑞銀': '外資', '瑞士信貸': '外資', '瑞信': '外資',
    '德意志': '外資', '花旗': '外資', '匯豐': '外資', '麥格理': '外資',
    '野村': '外資', '巴克萊': '外資', '法國巴黎': '外資', '里昂': '外資',
    '元大': '本土大型', '凱基': '本土大型', '富邦': '本土大型',
    '國泰': '本土大型', '永豐金': '本土大型', '玉山': '本土大型',
    '群益': '本土大型', '兆豐': '本土大型', '中信': '本土大型',
}

def _broker_tag(name):
    for kw, tag in _BROKER_TAGS.items():
        if kw in name:
            return tag
    return ''

def _recent_trading_dates(n=7):
    from datetime import date, timedelta
    d = date.today() - timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return out

def _t86_parse_int(v):
    s = str(v).strip().replace(',', '')
    return int(s) if s and s.lstrip('-').isdigit() else 0

def _fetch_t86_for_stock(stock_no, date_str, hdrs):
    """Fetch T86 三大法人 and return one stock's row, or None."""
    r   = _requests.get('https://www.twse.com.tw/rwd/zh/fund/T86',
                        params={'date': date_str, 'selectType': 'ALLBUT0999', 'response': 'json'},
                        headers=hdrs, timeout=8)
    jd  = r.json()
    if jd.get('stat') != 'OK':
        return None
    for row in (jd.get('data') or []):
        if str(row[0]).strip() == stock_no:
            p = _t86_parse_int
            return {
                'date':    date_str,
                'foreign': p(row[4])  + p(row[7]),   # 外陸資超 + 外資自營超
                'trust':   p(row[10]),                # 投信超
                'dealer':  p(row[11]),                # 自營商買賣超（合計，index 11）
                'total':   p(row[18]),                # 三大法人買賣超總計（index 18）
            }
    return None

def _fetch_tpex_3insti(stock_no, date_str, hdrs):
    """Fetch TPEX 三大法人 for an OTC stock, or None."""
    yr  = int(date_str[:4]) - 1911
    dp  = f'{yr}/{date_str[4:6]}/{date_str[6:8]}'
    r   = _requests.get(
            'https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge.php',
            params={'d': dp, 'stkno': stock_no, 'o': 'json'},
            headers=hdrs, timeout=8)
    jd  = r.json()
    for row in (jd.get('aaData') or []):
        if str(row[0]).strip() == stock_no:
            p = _t86_parse_int
            return {
                'date':    date_str,
                'foreign': p(row[4]),
                'trust':   p(row[7]),
                'dealer':  p(row[10]),
                'total':   p(row[4]) + p(row[7]) + p(row[10]),
            }
    return None


@app.route('/api/tw/broker_chips/<ticker>')
def get_tw_broker_chips(ticker):
    raw      = tw_normalize(ticker)
    stock_no = raw.split('.')[0]
    is_otc   = raw.endswith('.TWO')

    cache_key = f'tw_broker:{stock_no}'
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)

    hdrs = {
        'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept':          'application/json',
        'X-Requested-With':'XMLHttpRequest',
    }
    dates    = _recent_trading_dates(8)
    day_data = []

    for date_str in dates:
        if len(day_data) >= 5:
            break
        try:
            row = (_fetch_tpex_3insti if is_otc else _fetch_t86_for_stock)(stock_no, date_str, hdrs)
            if row:
                day_data.append(row)
        except Exception:
            pass

    agg_f = sum(d['foreign'] for d in day_data)
    agg_t = sum(d['trust']   for d in day_data)
    agg_d = sum(d['dealer']  for d in day_data)
    agg_total = sum(d['total'] for d in day_data)

    result = {
        'stockNo':  stock_no,
        'hasData':  len(day_data) > 0,
        'days':     day_data,
        'aggregate': {'foreign': agg_f, 'trust': agg_t, 'dealer': agg_d, 'total': agg_total},
    }
    _cache_set(cache_key, result, ttl=3600)
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5999, debug=False)
