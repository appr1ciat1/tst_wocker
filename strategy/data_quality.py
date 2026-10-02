"""
strategy.data_quality — 行情資料與訂單的品質關卡（無網路依賴，可單元測試）

起因：2026-09-30 / 10-01 訂單混入不存在的代號（3529O、5274O），
且 10/01 用的是空白 bar 補出來的假價格，整個流程沒有任何警訊。

本模組集中處理：
- normalize_tw_symbol：只剝除結尾的 .TWO / .TW
- drop_phantom_bars：丟棄「沒有有效行情」的日期（休市或資料未發布）
- expected_latest_session：此刻應該已經有資料的最新台股交易日
- alert：同時印到 console 與 GitHub Actions annotation（顯示在 run 摘要頁）
"""

import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

_TW_SUFFIX_RE = re.compile(r'\.(TWO|TW)$')

# 合法台股代號：4~6 位數字，可帶一個大寫字母（ETF 如 00631L、特別股如 2881A）。
# 注意：'3529O' 也符合此格式，所以「格式檢查」擋不住本次事故，必須做「股池成員檢查」。
_TW_CODE_RE = re.compile(r'^\d{4,6}[A-Z]?$')

# 一個日期的有效 bar（Close 非空且 Volume>0）比例低於此值 → 視為假 bar。
MIN_BAR_COVERAGE = 0.5

# 台股 13:30 收盤；yfinance 約台灣 16:00 後可取得當日日線。
# 08:30 UTC = 台灣 16:30。排程在 09:17 UTC，留 47 分鐘緩衝。
DATA_READY_UTC_HOUR = 8.5


def normalize_tw_symbol(symbol):
    """'3529.TWO' → '3529'、'2330.TW' → '2330'；只剝除結尾後綴。"""
    return _TW_SUFFIX_RE.sub('', str(symbol))


def is_valid_tw_code(code):
    return bool(_TW_CODE_RE.match(str(code)))


def _escape_annotation(msg):
    # GitHub workflow command 的保留字元需編碼，否則多行訊息會被截斷
    return str(msg).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def alert(msg, level='warning'):
    """印出警告；在 GitHub Actions 中另外輸出 annotation，讓警告出現在 run 摘要頁。

    level: 'notice' | 'warning' | 'error'
    """
    icon = {'error': '❌', 'warning': '⚠️', 'notice': 'ℹ️'}.get(level, '⚠️')
    print(f"   {icon} {msg}", flush=True)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print(f"::{level}::{_escape_annotation(msg)}", flush=True)


_BUY_ROW_RE = re.compile(r'<tr[^>]*>\s*<td>([^<]+)</td>((?:(?!</tr>).)*?)</tr>', re.DOTALL)


def report_buy_row_codes(html):
    """stock_report.html 中所有標為「建議買進」的列的代號（不做格式過濾）。

    給 HTML 後援解析器對帳用：解析出的訊號數 < 這裡的數量 → 有訂單被靜默丟掉。
    """
    return [code.strip() for code, body in _BUY_ROW_RE.findall(html) if '建議買進' in body]


def reconcile_parsed_signals(html, parsed_codes, source='stock_report.html'):
    """比對 HTML 中的買進列與實際解析成功的代號；不一致就發警告。回傳漏掉的代號。"""
    expected = report_buy_row_codes(html)
    parsed = {str(c) for c in parsed_codes}
    dropped = [c for c in expected if c not in parsed]
    if dropped:
        alert(f"{source} 有 {len(dropped)} 筆買進列解析失敗而被略過: {', '.join(dropped)}")
    bad = [c for c in expected if not is_valid_tw_code(c)]
    if bad:
        alert(f"{source} 出現格式不合法的代號: {', '.join(bad)}", level='error')
    return dropped


def filter_orders_by_universe(orders, universe, source='orders'):
    """依訂單檔內的 universe 拒收不在股池內的代號（schema_version>=2 才有 universe）。"""
    if not universe:
        return list(orders)
    allowed = {str(t) for t in universe}
    kept, rejected = [], []
    for o in orders:
        (kept if str(o.get('ticker')) in allowed else rejected).append(o)
    if rejected:
        alert(f"{source} 有 {len(rejected)} 筆訂單代號不在股池內，已拒收: "
              f"{', '.join(str(o.get('ticker')) for o in rejected)}", level='error')
    return kept


def drop_phantom_bars(close_raw, vol_raw=None, min_coverage=MIN_BAR_COVERAGE):
    """回傳 (保留的日期 index, 被丟棄的日期 list)。

    判定：當日 Close 非空且 Volume>0 的股票數 / 有資料的股票數 < min_coverage。
    必須在 ffill 之前判定，否則 Close 會被前一日值補上而偽裝成新的一天。
    """
    if close_raw.empty:
        return close_raw.index, []
    valid = close_raw.notna()
    if vol_raw is not None and not vol_raw.empty:
        vol_aligned = vol_raw.reindex(index=close_raw.index, columns=close_raw.columns)
        valid &= vol_aligned.fillna(0) > 0
    n_cols = max(int(close_raw.notna().any().sum()), 1)
    coverage = valid.sum(axis=1) / n_cols
    bad = coverage < min_coverage
    return close_raw.index[~bad], list(close_raw.index[bad])


_ROUTINE_RUNTIME_MSGS = (
    r'(Mean of empty slice|All-NaN slice|invalid value encountered|'
    r'divide by zero encountered|Degrees of freedom <= 0)'
)


def quiet_routine_warnings():
    """只靜音面板運算中例行出現的雜訊警告，其餘（UserWarning 等）照常顯示。

    取代各模組的 warnings.filterwarnings('ignore')：那會在 import 時全域吞掉
    整個 process 的所有警告，資料異常因此完全看不到。
    """
    import warnings
    warnings.filterwarnings('ignore', category=FutureWarning)
    warnings.filterwarnings('ignore', category=DeprecationWarning)
    warnings.filterwarnings('ignore', category=RuntimeWarning, message=_ROUTINE_RUNTIME_MSGS)


def _xtai_calendar():
    import exchange_calendars as xcals
    return xcals.get_calendar('XTAI')


def expected_latest_session(now_utc=None, calendar=None, ready_hour_utc=DATA_READY_UTC_HOUR):
    """此刻「應該已經有日線資料」的最新 XTAI 交易日（tz-naive Timestamp）。

    交易日 D 的資料在 D 當天 ready_hour_utc（UTC）之後才算就緒。
    例：UTC 2026-10-01 16:32（台灣 10/02 00:32）→ 10/02 尚未就緒 → 回傳 10/01。
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    cal = calendar or _xtai_calendar()
    end = pd.Timestamp(now_utc.date())
    sessions = cal.sessions_in_range(end - pd.Timedelta(days=30), end)
    for s in reversed(list(sessions)):
        s = pd.Timestamp(s).tz_localize(None) if pd.Timestamp(s).tzinfo else pd.Timestamp(s)
        if s + pd.Timedelta(hours=ready_hour_utc) <= pd.Timestamp(now_utc):
            return s.normalize()
    return None


def report_reference_date(now_utc=None):
    """排程判斷「今天要不要跑」用的台灣日期。

    以台灣時間往回推 12 小時：台灣 12:00 ~ 隔日 11:59 都對應到同一個交易日。
    避免排程延遲跨過台灣午夜時，把週五的 run 誤判成週六（非交易日）而跳過。
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    return (now_utc + timedelta(hours=8) - timedelta(hours=12)).date()


def check_data_freshness(latest_date, now_utc=None, calendar=None):
    """回傳 (is_fresh, expected_session)。無法判定（無行事曆）時視為新鮮並回傳 None。"""
    try:
        expected = expected_latest_session(now_utc=now_utc, calendar=calendar)
    except Exception as e:  # exchange_calendars 不可用
        alert(f"無法取得 XTAI 行事曆，略過資料新鮮度檢查: {e}")
        return True, None
    if expected is None:
        return True, None
    return pd.Timestamp(latest_date).normalize() >= expected, expected
