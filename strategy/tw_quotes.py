"""
strategy.tw_quotes — 交易所官方日行情（TWSE 上市、TPEx 上櫃）

用途：Yahoo 偶爾整天缺資料（2025-08-01、2026-10-12 已證實），
此時改向交易所抓當天行情，補回缺日（換算見 strategy.gap_fill）。

回傳「未還原」的原始價，另帶參考價 ref = 收盤 − 漲跌。
參考價就是除權息後的開盤基準價，因此不需知道股利金額，
就能把原始價換算到 Yahoo 的還原價尺度。

本模組只負責抓取與解析；所有判斷邏輯放在 gap_fill（可離線測試）。
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

TWSE_URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            "?date={ymd}&type=ALLBUT0999&response=json")
# TPEx 2024 年改版過網站；新舊兩個端點都試，以回傳日期相符者為準。
TPEX_URLS = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
    "?date={y}%2F{m}%2F{d}&id=&response=json",
    "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
    "?l=zh-tw&o=json&d={roc}/{m}/{d}&s=0,asc,0",
)

QUOTE_COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'ref']

# TWSE 憑證缺 SKI 擴展，OpenSSL 3 的嚴格檢查會擋；只放寬這一項，仍驗證憑證鏈。
_CTX = ssl.create_default_context()
_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

REQUEST_PAUSE = float(os.environ.get('TW_QUOTE_PAUSE', '3.0'))
_RETRY_WAITS = (8, 25)
_RETRY_CODES = {307, 403, 429, 500, 502, 503, 504}
_last_request = [0.0]


class NoData(Exception):
    """交易所明確回覆「當日無資料」（休市，或該市場當天沒有這份報表）。"""


# ───────────────────────────── 解析（純函式） ─────────────────────────────

def _num(x):
    """'1,234.50' → 1234.5；'--'、''、'X' 等 → NaN。"""
    if x is None:
        return np.nan
    s = re.sub(r'<[^>]+>', '', str(x)).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return np.nan


def _strip_tags(x):
    return re.sub(r'<[^>]+>', '', str(x or '')).strip()


def _norm_field(f):
    return re.sub(r'\s+', '', str(f))


def _col(fields, *names):
    norm = [_norm_field(f) for f in fields]
    for n in names:
        if n in norm:
            return norm.index(n)
    return None


def _frame(rows):
    df = pd.DataFrame(rows, columns=['code'] + QUOTE_COLUMNS)
    df = df[df['close'].notna() & (df['close'] > 0)]
    return df.drop_duplicates('code').set_index('code')


def parse_twse(payload, ymd):
    """MI_INDEX（ALLBUT0999）→ DataFrame(index=代號, columns=QUOTE_COLUMNS)。

    休市日 TWSE 回 stat='很抱歉，沒有符合條件的資料!' → NoData。
    其他非 OK 訊息（例如限流）→ RuntimeError，不可當成休市。
    """
    stat = str(payload.get('stat', ''))
    if stat.upper() != 'OK':
        if '沒有符合條件' in stat or '查無' in stat:
            raise NoData(f'TWSE {ymd}: {stat}')
        raise RuntimeError(f'TWSE {ymd} 異常回應（疑似限流）: {stat}')
    if payload.get('date') and str(payload['date']) != ymd:
        raise RuntimeError(f"TWSE 回傳日期 {payload['date']} 與請求 {ymd} 不符")
    for t in payload.get('tables') or []:
        f = t.get('fields') or []
        i_code, i_close = _col(f, '證券代號'), _col(f, '收盤價')
        if i_code is None or i_close is None or not t.get('data'):
            continue
        i_open, i_high, i_low = _col(f, '開盤價'), _col(f, '最高價'), _col(f, '最低價')
        i_vol = _col(f, '成交股數')
        i_sign, i_chg = _col(f, '漲跌(+/-)'), _col(f, '漲跌價差')
        if None in (i_open, i_high, i_low, i_vol, i_sign, i_chg):
            raise ValueError(f'TWSE 欄位無法辨識: {f}')
        rows = []
        for r in t['data']:
            close = _num(r[i_close])
            sign, chg = _strip_tags(r[i_sign]), _num(r[i_chg])
            if sign == '+':
                ref = close - chg
            elif sign == '-':
                ref = close + chg
            elif sign == '' and chg == 0:
                ref = close
            else:                       # 'X'（不比價）等：參考價未知
                ref = np.nan
            rows.append([str(r[i_code]).strip(), _num(r[i_open]), _num(r[i_high]),
                         _num(r[i_low]), close, _num(r[i_vol]), ref])
        return _frame(rows)
    # stat=OK 卻沒有個股表：多半是 TWSE 改版或回應不完整。不可當成休市。
    raise RuntimeError(f'TWSE {ymd}: stat=OK 但找不到個股收盤行情表（格式可能改版）')


# 舊版 TPEx（aaData，無 fields）固定欄位：代號,名稱,收盤,漲跌,開盤,最高,最低,均價,成交股數,...
_TPEX_LEGACY = ['代號', '名稱', '收盤', '漲跌', '開盤', '最高', '最低', '均價', '成交股數']


def _tpex_date_ok(payload, ymd):
    """回傳 True/False；沒有任何日期欄位時回傳 None（無法核對）。"""
    d = payload.get('date')
    if d:
        return re.sub(r'\D', '', str(d)) == ymd
    rd = payload.get('reportDate')
    if rd:
        p = re.findall(r'\d+', str(rd))
        if len(p) == 3:
            y = int(p[0]) + (1911 if int(p[0]) < 1911 else 0)
            return f'{y:04d}{int(p[1]):02d}{int(p[2]):02d}' == ymd
    return None


def parse_tpex(payload, ymd):
    """TPEx 上櫃每日收盤行情（新舊兩版格式）→ DataFrame。

    TPEx 在非交易日可能回傳「最近一個交易日」的資料，因此必須核對日期；
    日期不符或無法核對 → 不採用。
    """
    ok = _tpex_date_ok(payload, ymd)
    if ok is False:
        raise NoData(f'TPEx 回傳日期與 {ymd} 不符')
    if ok is None:
        raise RuntimeError(f'TPEx {ymd}: 回應沒有日期欄位，無法核對，不採用')
    tables = []
    for t in payload.get('tables') or []:
        tables.append((t.get('fields') or [], t.get('data') or []))
    if payload.get('aaData') is not None:
        tables.append((payload.get('fields') or _TPEX_LEGACY, payload['aaData']))
    for f, data in tables:
        i_code, i_close = _col(f, '代號'), _col(f, '收盤')
        if i_code is None or i_close is None:
            continue
        if not data:
            raise NoData(f'TPEx {ymd}: 無資料')
        i_chg, i_open = _col(f, '漲跌'), _col(f, '開盤')
        i_high, i_low, i_vol = _col(f, '最高'), _col(f, '最低'), _col(f, '成交股數')
        if None in (i_chg, i_open, i_high, i_low, i_vol):
            raise ValueError(f'TPEx 欄位無法辨識: {f}')
        rows = []
        for r in data:
            close, chg = _num(r[i_close]), _num(r[i_chg])
            rows.append([str(r[i_code]).strip(), _num(r[i_open]), _num(r[i_high]),
                         _num(r[i_low]), close, _num(r[i_vol]), close - chg])
        return _frame(rows)
    raise NoData(f'TPEx {ymd}: 找不到收盤行情表')


# ───────────────────────────── 抓取 ─────────────────────────────

# 斷路器：同一主機連續「連線層」失敗達門檻（逾時、307/403/429/5xx，例如 IP 被暫時封鎖），
# 就停止再打該主機，避免每個缺日都等滿重試。狀態寫進快取目錄，
# 讓同一個 runner 上後續的 ai_report（4 個策略）也立刻知道。404 等明確錯誤不計入。
BREAKER_THRESHOLD = 2
BREAKER_SECONDS = 30 * 60
_fail_streak = {}


def _host(url):
    return urllib.parse.urlsplit(url).hostname or 'unknown'


def _breaker_path(host):
    return os.path.join(os.path.dirname(_cache_path('x', 'x')), f'_breaker_{host}')


def _breaker_open(host):
    if _fail_streak.get(host, 0) >= BREAKER_THRESHOLD:
        return True
    p = _breaker_path(host)
    return os.path.exists(p) and time.time() - os.path.getmtime(p) < BREAKER_SECONDS


def _is_transport_failure(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _RETRY_CODES
    # TWSE 封鎖時以 307 轉址到 HTML 擋頁，urllib 會跟隨轉址 → 解析 JSON 失敗，也算連線層失敗
    return isinstance(e, (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError))


def _http_get_json(url, timeout=60):
    host = _host(url)
    if _breaker_open(host):
        raise RuntimeError(f'{host} 連線連續失敗，斷路器開啟中（{BREAKER_SECONDS // 60} 分鐘內不再嘗試）')
    try:
        payload = _http_get_json_retry(url, timeout)
    except Exception as e:
        if _is_transport_failure(e):
            _fail_streak[host] = _fail_streak.get(host, 0) + 1
            if _fail_streak[host] >= BREAKER_THRESHOLD:
                os.makedirs(os.path.dirname(_breaker_path(host)), exist_ok=True)
                open(_breaker_path(host), 'w').close()
        raise
    _fail_streak[host] = 0
    return payload


def _http_get_json_retry(url, timeout=60):
    last = None
    for wait in (0,) + _RETRY_WAITS:
        if wait:
            time.sleep(wait)
        gap = time.time() - _last_request[0]
        if gap < REQUEST_PAUSE:
            time.sleep(REQUEST_PAUSE - gap)
        _last_request[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'tst-wocker/1.0'})
            with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in _RETRY_CODES:
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            continue
    raise last


_get_json = _http_get_json      # 測試以 monkeypatch 取代此名稱


def _cache_path(source, ymd):
    root = os.environ.get('TW_QUOTE_CACHE', os.path.join('.cache', 'tw_quotes'))
    return os.path.join(root, f'{source}_{ymd}.json')


def _cache_load(source, ymd):
    p = _cache_path(source, ymd)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as fh:
        c = json.load(fh)
    if c.get('status') == 'no_data':
        return 'no_data'
    df = pd.DataFrame(c['rows'], columns=['code'] + QUOTE_COLUMNS).set_index('code')
    return df


def _cache_save(source, ymd, df_or_status):
    p = _cache_path(source, ymd)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(df_or_status, str):
        c = {'status': df_or_status}
    else:
        c = {'status': 'ok',
             'rows': df_or_status.reset_index().astype(object)
                     .where(df_or_status.reset_index().notna(), None).values.tolist()}
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(c, fh)


def _fetch_cached(source, ymd, fetch, cache_no_data):
    hit = _cache_load(source, ymd)
    if isinstance(hit, pd.DataFrame):
        return hit
    if hit == 'no_data':
        raise NoData(f'{source} {ymd}: 無資料（快取）')
    try:
        df = fetch()
    except NoData:
        if cache_no_data:
            _cache_save(source, ymd, 'no_data')
        raise
    _cache_save(source, ymd, df)
    return df


def fetch_twse(date, cache_no_data=True):
    ymd = pd.Timestamp(date).strftime('%Y%m%d')
    return _fetch_cached('twse', ymd,
                         lambda: parse_twse(_get_json(TWSE_URL.format(ymd=ymd)), ymd),
                         cache_no_data)


def fetch_tpex(date, cache_no_data=True):
    ts = pd.Timestamp(date)
    ymd = ts.strftime('%Y%m%d')

    def fetch():
        errors = []
        for tmpl in TPEX_URLS:
            url = tmpl.format(y=ts.year, m=f'{ts.month:02d}', d=f'{ts.day:02d}', roc=ts.year - 1911)
            try:
                return parse_tpex(_get_json(url), ymd)
            except Exception as e:   # 換下一個端點
                errors.append(f'{type(e).__name__}: {e}')
        if all(e.startswith('NoData') for e in errors):
            raise NoData(f'TPEx {ymd}: ' + ' | '.join(errors))
        raise RuntimeError(f'TPEx {ymd} 全部端點失敗: ' + ' | '.join(errors))

    return _fetch_cached('tpex', ymd, fetch, cache_no_data)


def fetch_day(date, codes):
    """抓某日官方行情，涵蓋 codes 中的上市與上櫃股。

    回傳 (status, DataFrame, notes)：
    - status='closed'：TWSE 明確回覆無資料（證交所為權威來源，上櫃與上市同休）。
    - status='ok'：DataFrame 含找得到的代號；找不到的代號不在 index 內。
    網路失敗或回應異常 → 拋出例外（呼叫端視為「無法確認」，不可當成休市）。
    """
    ts = pd.Timestamp(date).normalize()
    # 只有夠舊的日期才快取「無資料」，避免把「尚未發布」誤存成休市
    cache_no_data = ts < pd.Timestamp.now().normalize() - pd.Timedelta(days=3)
    notes = []
    try:
        twse = fetch_twse(ts, cache_no_data)
    except NoData:
        return 'closed', pd.DataFrame(columns=QUOTE_COLUMNS), notes
    want = [str(c) for c in codes]
    out = twse.loc[[c for c in want if c in twse.index]].assign(source='TWSE')
    rest = [c for c in want if c not in twse.index]
    unchecked = []
    if rest:
        try:
            tpex = fetch_tpex(ts, cache_no_data)
            out = pd.concat([out, tpex.loc[[c for c in rest if c in tpex.index]].assign(source='TPEx')])
        except Exception as e:
            # TWSE 有開市時 TPEx 一定也有開市；TPEx 回「無資料」同樣代表取得失敗，不可當成停牌
            unchecked = rest
            notes.append(f'TPEx 取得失敗（{len(rest)} 檔無法確認）: {e}')
    # 查無的代號：在 unchecked 內 → 無法確認；其餘 → 當天無成交（停牌等）
    out.attrs['unchecked'] = unchecked
    return 'ok', out, notes
