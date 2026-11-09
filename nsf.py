"""國安基金（國家金融安定基金）歷次進出場與進場後的大盤表現。

國安基金由委員會開會決議進退場，不即時公布每筆買賣，也沒有可以串接的資料來源，
所以這裡是「事件紀錄」不是跟單：歷次起訖、金額、損益整理自媒體報導（官方未提供同格式的一覽表），
指數報酬一律用 Yahoo 的加權指數收盤價自己算（只算價格、不含股息）。
新決議由管理員在頁面上記錄，會推播給開啟提醒的帳號。

各來源有出入的地方：第 1～4、7、9 次的進場日差一天（決議日與實際進場日的差別）；
第 1 次的 500 億虧損多數報導寫虧損、部分寫帳面一度虧損，立法院資料只有前三次合計（獲利 61 億）；
第 8 次淨利有 83、112.96、118.19 億三種說法。表內 note 欄都有標註。
"""
import os
import re
import json
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
from flask import Blueprint, jsonify, request, render_template

import auth
import fear
import notify
import tw_board
import userdata

bp = Blueprint('nsf', __name__)

HORIZONS = [(5, '1 週'), (21, '1 個月'), (63, '3 個月'), (126, '6 個月'), (252, '12 個月')]
ALERT_KEY = 'nsf_alerts'
UPDATE_KEY = 'nsf_updates'                # 管理員記錄的新決議（存在系統帳號），會蓋過下面的內建資料
SITE_URL = fear.SITE_URL

ROUNDS = [
    {'n': 1, 'days': 5, 'entry': '2000-03-16', 'exit': '2000-03-20', 'reason': '總統大選、台海情勢緊張', 'amount': 542.0, 'pnl': -500.0,
     'note': '虧損 500 億多數報導如此記載；立法院資料只列前三次合計獲利 61 億。進場日另有 3/15 的說法。'},
    {'n': 2, 'days': 43, 'entry': '2000-10-03', 'exit': '2000-11-15', 'reason': '網路泡沫破裂', 'amount': 1227.0, 'pnl': 226.0,
     'note': '歷次動用金額最大。進場日另有 10/2 的說法。'},
    {'n': 3, 'days': 12, 'entry': '2004-05-20', 'exit': '2004-06-01', 'reason': '319 槍擊案後政局不安', 'amount': 16.0, 'pnl': 35.0,
     'note': '進場日另有 5/19 的說法。'},
    {'n': 4, 'days': 90, 'entry': '2008-09-18', 'exit': '2008-12-17', 'reason': '雷曼兄弟倒閉、金融海嘯', 'amount': 600.0, 'pnl': 319.23,
     'note': '歷次獲利金額最高。進場日另有 9/19、9/20 的說法（報導的進場前收盤 5800.87 是 9/17，對應 9/18 進場）；退場日只有一家來源。'},
    {'n': 5, 'days': 120, 'entry': '2011-12-21', 'exit': '2012-04-20', 'reason': '歐債危機', 'amount': 424.0, 'pnl': 37.01, 'note': ''},
    {'n': 6, 'days': 232, 'entry': '2015-08-25', 'exit': '2016-04-12', 'reason': '中國股災、全球股市重挫', 'amount': 196.58, 'pnl': 12.11,
     'note': '退場日只有一家來源，與 232 天相符。'},
    {'n': 7, 'days': 207, 'entry': '2020-03-20', 'exit': '2020-10-12', 'reason': '新冠疫情、油價暴跌', 'amount': 7.57, 'pnl': 2.58,
     'note': '歷次動用金額最少。全數出清另有 2020-11-09 的記載。'},
    {'n': 8, 'days': 275, 'entry': '2022-07-13', 'exit': '2023-04-13', 'reason': '通膨升息、俄烏戰爭', 'amount': 545.08, 'pnl': 112.96,
     'note': '淨利另有 83、118.19 億的說法。持股於 2023-12-25 全數出清。'},
    {'n': 9, 'days': 279, 'entry': '2025-04-09', 'exit': '2026-01-12', 'reason': '美國對等關稅', 'amount': 122.5, 'pnl': 99.32,
     'note': '4/8 決議、4/9 開始護盤；持股於 2026-05-06 全數出清。損益為淨利。'},
    {'n': 10, 'entry': '2026-11-09', 'exit': None, 'decided': '2026-11-06', 'reason': '台股單週重挫、投資信心不足', 'amount': None, 'pnl': None,
     'note': '11/6 第 131 次臨時委員會決議，授權執行秘書視情況動用資金；報導指 11/9 起進場。實際金額與標的未公布。'},
]


def rounds():
    """內建資料 + 管理員的更新。"""
    upd = userdata.get(userdata.SYSTEM, UPDATE_KEY) or {}
    out = {r['n']: dict(r) for r in ROUNDS}
    for k, v in upd.items():
        n = int(k)
        out[n] = dict(out.get(n, {'n': n, 'note': ''}), **v)
    return [out[k] for k in sorted(out)]


def _pos(idx, d):
    """第一個 >= d 的交易日位置。"""
    k = idx.searchsorted(pd.Timestamp(d))
    return int(k) if k < len(idx) else None


def analyze():
    px = fear._hist('^TWII')
    v, idx = px.values, px.index
    out, paths = [], []
    for r in rounds():
        e = dict(r, status='open' if not r.get('exit') else 'closed')
        k = _pos(idx, r['entry'])
        prev_i = k - 1 if k else len(v) - 1                      # 進場前一個交易日收盤：看進場時已經跌了多少
        e['prev_close'] = round(float(v[prev_i]), 2)
        hi_win = v[max(0, prev_i - 252):prev_i + 1]
        e['from_high'] = round((v[prev_i] / hi_win.max() - 1) * 100, 1)
        if k is not None:
            e['entry_close'] = round(float(v[k]), 2)
            e['first_day'] = round((v[k] / v[prev_i] - 1) * 100, 1)
            end = _pos(idx, r['exit']) if r.get('exit') else len(v) - 1
            end = len(v) - 1 if end is None else end
            e['exit_close'] = round(float(v[end]), 2)
            e['ret_hold'] = round((v[end] / v[k] - 1) * 100, 1)
            low = int(np.argmin(v[k:end + 1])) + k
            e['low_after'] = round((v[low] / v[k] - 1) * 100, 1)
            e['low_day'] = low - k
            for h, _ in HORIZONS:
                e[f'r{h}'] = round((v[k + h] / v[k] - 1) * 100, 1) if k + h < len(v) else None
            lim = min(len(v), k + 127)
            paths.append({'n': r['n'], 'y': [round((x / v[k] - 1) * 100, 2) for x in v[k:lim]]})
        if not r.get('exit'):
            e['days'] = (dt.date.today() - dt.date.fromisoformat(r['entry'])).days
        elif not r.get('days'):
            e['days'] = (dt.date.fromisoformat(r['exit']) - dt.date.fromisoformat(r['entry'])).days
        out.append(e)

    # 統計：已知報酬的每一次，對照任何一天買進
    stats = []
    for h, name in HORIZONS:
        rs = [e[f'r{h}'] for e in out if e.get(f'r{h}') is not None]
        base = v[h:] / v[:-h] - 1
        stats.append({'h': name, 'n': len(rs), 'avg': round(float(np.mean(rs)), 1) if rs else None,
                      'median': round(float(np.median(rs)), 1) if rs else None,
                      'win': round(float(np.mean(np.array(rs) > 0)) * 100, 0) if rs else None,
                      'worst': round(float(min(rs)), 1) if rs else None, 'best': round(float(max(rs)), 1) if rs else None,
                      'base_avg': round(float(base.mean()) * 100, 1), 'base_win': round(float((base > 0).mean()) * 100, 0)})
    closed = [e for e in out if e['status'] == 'closed' and e.get('low_after') is not None]
    return {'rounds': out, 'stats': stats, 'paths': paths,
            'open': next((e for e in out if e['status'] == 'open'), None),
            'last': {'date': str(idx[-1].date()), 'close': round(float(v[-1]), 2), 'live': tw_board._market_open()},
            'lows': {'avg': round(float(np.mean([e['low_after'] for e in closed])), 1),
                     'avg_day': round(float(np.mean([e['low_day'] for e in closed])), 0),
                     'first_day_low': sum(1 for e in closed if e['low_day'] == 0), 'n': len(closed)} if closed else None,
            'index': [[str(d.date()), round(float(x), 2)] for d, x in px.iloc[::5].items()] + [[str(idx[-1].date()), round(float(v[-1]), 2)]]}


# ── 八大公股行庫買賣超（參考指標，不是國安基金的買賣）────────────────
# 國安基金不公布每日買賣，市場常以八大行庫券商的合計進出推測政府資金動向；任何人都能在這些券商開戶，
# 所以含一般客戶委託，各家媒體的統計口徑也不同。資料來自 HiStock 的整理頁，30 分鐘最多抓一次，
# 每日合計累積存在硬碟（來源只給近 120 個交易日）。
BANKS_URL = 'https://histock.tw/stock/broker8.aspx'
BANK_NAMES = ['合庫', '土銀', '台銀', '台企銀', '彰銀', '第一金', '兆豐銀', '華南永昌']
BANKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.nsf_cache', 'banks.json')
BANKS_TTL = 1800


def _parse_banks(html):
    i = html.find('loadChart({')
    if i < 0:
        raise ValueError('找不到每日合計資料')
    chart = json.JSONDecoder().raw_decode(html[i + len('loadChart('):])[0]
    day = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%d')
    series = {day(t): round(float(v), 2) for t, v in json.loads(chart['SumMoney'])}
    if not series:
        raise ValueError('每日合計是空的')

    def top(word):
        rows, date = [], None
        for m in re.finditer(r'<li[^>]*title="(\d{4}-\d\d-\d\d) 八大公股行庫合計' + word + r' Top\.(\d+) : (.+?) \((\w+)\) 合計：([-\d,]+)萬元"[^>]*>(.*?)</li>', html, re.S):
                date = m.group(1)
                cells = [int(x.replace(',', '')) for x in re.findall(r'<span class="w70">([-\d,]+)</span>', m.group(6))]
                rows.append({'rank': int(m.group(2)), 'name': m.group(3), 'code': m.group(4), 'total': round(int(m.group(5).replace(',', '')) / 1e4, 2),
                             'by_bank': dict(zip(BANK_NAMES, [round(c / 1e4, 2) for c in cells[:8]]))})
        return sorted(rows, key=lambda r: r['rank']), date
    buy, d1 = top('買超')
    sell, d2 = top('賣超')
    return {'series': series, 'buy': buy, 'sell': sell, 'list_date': d1 or d2}


def _load_banks():
    try:
        with open(BANKS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def banks():
    """回傳快取資料；過期才向來源抓，抓不到就用舊的並標 stale。"""
    cached = _load_banks()
    if cached and time.time() - cached.get('ts', 0) < BANKS_TTL:
        return cached
    try:
        r = requests.get(BANKS_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0 (compatible; StockLens)'})
        r.raise_for_status()
        new = _parse_banks(r.content.decode('utf-8', 'ignore'))
    except Exception as e:
        print(f'[nsf] banks: {e}')
        if cached:
            return dict(cached, stale=True)
        raise
    new['series'] = dict(sorted({**(cached or {}).get('series', {}), **new['series']}.items()))
    new['ts'] = time.time()
    os.makedirs(os.path.dirname(BANKS_FILE), exist_ok=True)
    with open(BANKS_FILE + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False)
    os.replace(BANKS_FILE + '.tmp', BANKS_FILE)
    return new


def banks_summary():
    b = banks()
    days = list(b['series'].items())
    vals = [v for _, v in days]
    streak = 0
    for v in reversed(vals):
        if v == 0 or (streak and (v > 0) != (streak > 0)):
            break
        streak += 1 if v > 0 else -1
    op = next((r for r in rounds() if not r.get('exit')), None)
    since = None
    if op:
        rows = [v for d, v in days if d >= op['entry']]
        since = {'n': op['n'], 'start': op['entry'], 'days': len(rows), 'sum': round(sum(rows), 1)}
    return {'date': days[-1][0], 'total': vals[-1], 'streak': streak, 'sum5': round(sum(vals[-5:]), 1), 'sum20': round(sum(vals[-20:]), 1),
            'since': since, 'series': days[-120:], 'buy': b['buy'][:15], 'sell': b['sell'][:15], 'list_date': b.get('list_date'),
            'banks': BANK_NAMES, 'stale': bool(b.get('stale')),
            'fetched': dt.datetime.fromtimestamp(b['ts'], dt.timezone(dt.timedelta(hours=8))).strftime('%m-%d %H:%M')}


# ── 記錄新決議與提醒 ──────────────────────────────────────────────────
def _is_admin():
    u = auth.current_user()
    return bool(u and u.get('role') == 'admin')


def _broadcast(text):
    for user, on in userdata.users_with(ALERT_KEY):
        if on and notify.ready(user):
            notify.send(user, text, url='/nsf')


def record(b):
    """管理員記錄：{'n', 'entry'|'exit', 'amount', 'pnl', 'note', 'reason'}；同一次可分次補資料。"""
    n = int(b['n'])
    known = {r['n']: r for r in rounds()}
    fields = {}
    for k in ('entry', 'exit', 'decided'):
        if b.get(k):
            dt.date.fromisoformat(b[k])
            fields[k] = b[k]
    for k in ('amount', 'pnl'):
        if b.get(k) not in (None, ''):
            fields[k] = float(b[k])
    for k in ('note', 'reason'):
        if b.get(k):
            fields[k] = str(b[k])[:300]
    if n not in known and 'entry' not in fields:
        raise ValueError('新的一次要填進場日')
    upd = userdata.get(userdata.SYSTEM, UPDATE_KEY) or {}
    upd[str(n)] = dict(upd.get(str(n), {}), **fields)
    userdata.put(userdata.SYSTEM, UPDATE_KEY, upd)
    old = known.get(n, {})
    if 'exit' in fields and not old.get('exit'):
        _broadcast(f"【StockLens 國安基金】第 {n} 次護盤退場（{fields['exit']}）\n{SITE_URL}/nsf")
    elif n not in known:
        _broadcast(f"【StockLens 國安基金】第 {n} 次進場（{fields['entry']}）\n{fields.get('reason', '')}\n歷次進場後表現：{SITE_URL}/nsf")


@bp.route('/api/nsf')
def api_nsf():
    try:
        res = analyze()
    except Exception as e:
        msg = 'Yahoo 暫時限制查詢次數，請過幾分鐘再試' if 'Too Many' in str(e) else f'資料讀取失敗：{e}'
        return jsonify(error=msg), 503
    user = userdata.current()
    res['alert'] = bool(userdata.get(user, ALERT_KEY))
    res['ready'] = notify.ready(user)
    res['admin'] = _is_admin()
    return jsonify(res)


@bp.route('/api/nsf/banks')
def api_banks():
    try:
        return jsonify(banks_summary())
    except Exception as e:
        return jsonify(error=f'八大行庫資料讀取失敗：{e}'), 503


@bp.route('/api/nsf/alert', methods=['POST'])
def api_alert():
    on = bool((request.get_json(silent=True) or {}).get('on'))
    userdata.put(userdata.current(), ALERT_KEY, on)
    return jsonify(alert=on)


@bp.route('/api/nsf/record', methods=['POST'])
def api_record():
    if not _is_admin():
        return jsonify(error='只有管理員可以記錄'), 403
    try:
        record(request.get_json(silent=True) or {})
    except (ValueError, KeyError, TypeError) as e:
        return jsonify(error=f'資料格式不正確：{e}'), 400
    return jsonify(ok=True)


def init(app, login_required):
    @app.route('/nsf')
    @login_required
    def nsf_page():
        return render_template('nsf.html')

    app.register_blueprint(bp)
