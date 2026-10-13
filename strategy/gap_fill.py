"""
strategy.gap_fill — 偵測並補回 Yahoo 整天缺漏的交易日

問題：Yahoo 偶爾整天沒有資料（2025-08-01、2026-10-12 已用 TWSE 證實是交易日）。
缺一天時，舊流程直接把前後兩天接在一起：
- N 日動能、均線、ATR 的窗口悄悄多涵蓋一天；
- 缺日的漲跌被併進下一天，當天報酬看起來是兩天的量；
- 原本的新鮮度關卡只看「最新一天」，對中間缺日完全沒有警訊。

做法：
1. 以 XTAI 行事曆列出區間內的交易日，找出 Yahoo 沒有的日期。
2. 向 TWSE（上市）/ TPEx（上櫃）抓當日官方行情。TWSE 回覆無資料 → 實際休市（行事曆錯誤），略過。
3. 原始價 → Yahoo 還原價：k = Yahoo 前一日還原收盤 / 當日參考價（參考價 = 收盤 − 漲跌）。
   Yahoo 的除權息調整使「還原報酬 = 收盤 / 參考價」，所以這個換算在除權息當天也成立。
4. 另用「下一個交易日」反推一次（k' = Yahoo 次日還原收盤 × 次日參考價 / 次日收盤 / 當日收盤）。
   兩者相差超過 tol → 該檔不補（Yahoo 或交易所某一方有誤），並列出代號。
5. 補不起來的缺日 → 回報 unfilled，由呼叫端決定中止或警告。

本模組不碰網路：行情由呼叫端傳入的 get_quotes(date) 提供，因此可離線測試。
"""

import sys

import numpy as np
import pandas as pd

from strategy.data_quality import alert, expected_latest_session

# 前向與反向換算因子容許的相對誤差。交易所價格只有 2 位小數，低價股的進位誤差約 0.1%。
FACTOR_TOL = 0.005
# 台股漲跌幅上限 10%；收盤 / 參考價超出此範圍代表參考價解析錯誤（新上市前五日除外，寧可不補）。
MAX_DAILY_MOVE = 0.105
# 補值後，當天有補到的股票數 / 前一日有行情的股票數 低於此值 → 視為沒補起來。
MIN_FILL_COVERAGE = 0.5
# 缺日數量異常多 → 多半是資料源或行事曆整體出問題，不嘗試逐日補（避免狂打交易所）。
MAX_GAPS = 40
# 補不起來的缺日落在最近這麼多個交易日內 → 會影響今日訊號（動能、均線、ATR 窗口）。
RECENT_WINDOW = 260


def _naive(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def candidate_sessions(first_bar, end_exclusive, calendar, now_utc=None):
    """[first_bar, end_exclusive) 之間、且此刻資料應已就緒的 XTAI 交易日。"""
    last = (_naive(end_exclusive) - pd.Timedelta(seconds=1)).normalize()
    ready = expected_latest_session(now_utc=now_utc, calendar=calendar)
    if ready is not None and ready < last:
        last = ready
    first = _naive(first_bar).normalize()
    if last < first:
        return []
    return [_naive(s).normalize() for s in calendar.sessions_in_range(first, last)]


def missing_sessions(index, sessions):
    have = set(pd.DatetimeIndex(index).normalize())
    return [s for s in sessions if s not in have]


def _fin(x):
    return x is not None and np.isfinite(x) and x > 0


def _prices_ok(q):
    """單檔原始行情的自洽檢查：價格為正、高低價包住開收。"""
    o, h, l, c = q['open'], q['high'], q['low'], q['close']
    if not all(_fin(v) for v in (o, h, l, c)):
        return False
    return h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9


def _ref_ok(q):
    """參考價可用：為正，且收盤相對參考價不超過漲跌幅（超過代表解析錯誤，寧可不用）。"""
    r = q['ref']
    return _fin(r) and abs(q['close'] / r - 1) <= MAX_DAILY_MOVE


def fill_gaps(raw, gaps, get_quotes, allow_trailing=False, tol=FACTOR_TOL,
              min_coverage=MIN_FILL_COVERAGE):
    """補回缺日。回傳 (新的 raw dict, 每個缺日的結果 list)。

    raw：{'Close','Open','High','Low','Volume'} → DataFrame(日期 × 代號)，ffill 之前、已剔除假 bar。
    gaps：缺少的交易日（升冪）。
    get_quotes(date) → (status, DataFrame, notes)；status 'closed' 或 'ok'；失敗時拋例外。
    allow_trailing：最後一根 Yahoo bar 之後的缺日（只能單向換算）是否也補。

    換算因子（原始價 → Yahoo 還原價）：
    - 前向 k_f = Yahoo 前一列還原收盤 / 當日參考價。
    - 反向 k_b：由下一根 Yahoo bar 往回串接交易所的「參考價 / 收盤」，
      adj_g = adj_N × Π(ref_d / close_d)，d 走過 g 之後到 N 的每個交易日（含中間其他缺日）。
    兩者都有 → 必須一致；只有一個 → 採用並記為單向；都沒有 → 不補。
    """
    raw = {k: v.copy() for k, v in raw.items()}
    yahoo_close = raw['Close'].copy()
    yahoo_idx = yahoo_close.index
    codes = list(yahoo_close.columns)
    gaps = sorted(_naive(g).normalize() for g in gaps)

    fetched = {}

    def quotes(d):
        if d not in fetched:
            try:
                fetched[d] = get_quotes(d)
            except Exception as e:  # 網路或回應異常：無法確認，不可當成休市
                fetched[d] = ('error', None, [f'{type(e).__name__}: {e}'])
        return fetched[d]

    results = []
    statuses = {}
    for g in gaps:
        trailing = len(yahoo_idx) == 0 or g > yahoo_idx[-1]
        if trailing and not allow_trailing:
            statuses[g] = 'skipped_trailing'
            continue
        statuses[g] = quotes(g)[0]

    for g in gaps:
        res = {'date': g.strftime('%Y-%m-%d'), 'n_filled': 0, 'n_active': 0,
               'one_sided': 0, 'rejected': [], 'rejected_codes': [], 'missing': [], 'no_trade': [],
               'sources': {}, 'notes': []}
        st = statuses[g]
        if st == 'skipped_trailing':
            res['status'] = 'skipped_trailing'
            results.append(res)
            continue
        _, qg, notes = quotes(g)
        res['notes'] += notes
        if st == 'closed':
            res['status'] = 'closed'
            results.append(res)
            continue
        if st == 'error':
            res['status'] = 'unfilled'
            res['reason'] = '交易所行情取得失敗'
            results.append(res)
            continue

        close = raw['Close']
        before = close.index[close.index < g]
        if len(before) == 0:
            res['status'] = 'unfilled'
            res['reason'] = '缺日之前沒有任何 Yahoo 資料'
            results.append(res)
            continue
        p = before[-1]

        # 反向換算鏈：g 之後到下一根 Yahoo bar N 的每個交易日（中間缺日 + N）都需要交易所行情
        chain, n = None, None
        after = yahoo_idx[yahoo_idx > g]
        if len(after):
            n = after[0]
            mids = [x for x in gaps if g < x < n and statuses.get(x) != 'closed']
            if all(statuses.get(x) == 'ok' for x in mids):
                st_n, qn, notes_n = quotes(n)
                res['notes'] += notes_n
                if st_n == 'ok':
                    chain = [quotes(x)[1] for x in mids] + [qn]
                else:
                    res['notes'].append(f'次一交易日 {n:%Y-%m-%d} 行情取得失敗，只能單向換算')
            else:
                res['notes'].append('之後的缺日無法取得行情，只能單向換算')

        # 交易所無法確認的代號（例如上櫃行情取得失敗）；其餘查無者 = 當天無成交（停牌等）
        unchecked = set(qg.attrs.get('unchecked', []))
        row = {f: {} for f in raw}
        for code in codes:
            c_p = close.at[p, code]
            if not _fin(c_p):
                continue            # 前一日就沒有行情（停牌等），不屬於本次缺日
            res['n_active'] += 1
            if code not in qg.index:
                (res['missing'] if code in unchecked else res['no_trade']).append(code)
                continue
            q = qg.loc[code]
            if not _prices_ok(q):
                res['rejected'].append(f'{code}(行情不自洽)')
                res['rejected_codes'].append(code)
                continue
            k_f = c_p / q['ref'] if _ref_ok(q) else None
            k_b = None
            if chain is not None and _fin(yahoo_close.at[n, code]):
                val = yahoo_close.at[n, code]
                for qd in reversed(chain):
                    if code not in qd.index or not (_ref_ok(qd.loc[code]) and _fin(qd.at[code, 'close'])):
                        val = None
                        break
                    val *= qd.at[code, 'ref'] / qd.at[code, 'close']
                if val is not None:
                    k_b = val / q['close']
            if k_f is not None and k_b is not None and abs(k_f / k_b - 1) > tol:
                res['rejected'].append(f'{code}(前後換算不一致 {k_f:.4f}/{k_b:.4f})')
                res['rejected_codes'].append(code)
                continue
            k = k_f if k_f is not None else k_b
            if k is None:
                res['rejected'].append(f'{code}(參考價不可用且無法反向換算)')
                res['rejected_codes'].append(code)
                continue
            if k_f is None or k_b is None:
                res['one_sided'] += 1
            row['Close'][code] = q['close'] * k
            if 'Open' in row:
                row['Open'][code] = q['open'] * k
            if 'High' in row:
                row['High'][code] = q['high'] * k
            if 'Low' in row:
                row['Low'][code] = q['low'] * k
            if 'Volume' in row:
                row['Volume'][code] = q['volume']
            src = q['source'] if 'source' in q.index else 'exchange'
            res['sources'][src] = res['sources'].get(src, 0) + 1
            res['n_filled'] += 1

        # 交易所回應涵蓋的檔數太少 → 回應可能不完整，不可把缺的都當成停牌
        present = res['n_active'] - len(res['missing']) - len(res['no_trade'])
        traded = res['n_active'] - len(res['no_trade'])
        if res['n_active'] and present / res['n_active'] < min_coverage:
            res['status'] = 'unfilled'
            res['reason'] = f'交易所回應只涵蓋 {present}/{res["n_active"]} 檔，疑似不完整'
            results.append(res)
            continue
        if res['n_filled'] == 0 and traded == 0:
            res['status'] = 'no_trade'     # 交易所當天有開市，但這些代號都沒有成交（停牌）
            results.append(res)
            continue
        if res['n_filled'] / max(traded, 1) < min_coverage or res['n_filled'] == 0:
            res['status'] = 'unfilled'
            res['reason'] = f'只補到 {res["n_filled"]}/{traded} 檔'
            results.append(res)
            continue
        for f, df in raw.items():
            new = pd.DataFrame([row[f]], index=pd.DatetimeIndex([g]), columns=df.columns, dtype=float)
            raw[f] = pd.concat([df, new]).sort_index()
        res['status'] = 'filled'
        results.append(res)
    return raw, results


def report_gap_results(results, label='行情', recent_cutoff=None):
    """把補值結果輸出到 log；會影響今日訊號的（recent_cutoff 之後）另發 Actions annotation。"""
    for r in results:
        d = r['date']
        recent = recent_cutoff is None or pd.Timestamp(d) >= recent_cutoff
        say = alert if recent else (lambda m, level='warning': print(f'   ℹ️ {m}'))
        for note in r.get('notes', []):
            say(f'{label} {d}: {note}')
        if r['status'] == 'closed':
            # 例行情況（颱風假等行事曆錯誤）：只印在 log，不佔用 Actions annotation 名額
            print(f'   ℹ️ {label}: XTAI 行事曆把 {d} 當交易日，但證交所當天無資料 → 實際休市，略過')
        elif r['status'] == 'filled':
            src = '、'.join(f'{k} {v}' for k, v in sorted(r['sources'].items()))
            say(f'{label}: Yahoo 缺 {d} 整天行情 → 已用交易所官方行情補上 '
                f'{r["n_filled"]}/{r["n_active"]} 檔（{src}；僅單向換算 {r["one_sided"]} 檔）')
            if r['missing']:
                say(f'{label} {d}: {len(r["missing"])} 檔無法向交易所確認（上櫃行情取得失敗），'
                    f'該日留空: {", ".join(r["missing"])}', level='error')
            if r['rejected']:
                say(f'{label} {d}: {len(r["rejected"])} 檔換算檢查未通過，該日留空: '
                    f'{", ".join(r["rejected"])}')
            if r['no_trade']:
                print(f'   ℹ️ {label} {d}: {len(r["no_trade"])} 檔當天無成交（停牌等），該日留空: '
                      f'{", ".join(r["no_trade"])}')
        elif r['status'] == 'no_trade':
            print(f'   ℹ️ {label}: {d} 交易所有開市，但此標的當天無成交（停牌等），略過')
        elif r['status'] == 'unfilled':
            say(f'{label}: Yahoo 缺 {d} 整天行情，且無法從交易所補回（{r.get("reason", "")}）。'
                f'動能、均線、ATR 會把前後兩天接在一起計算', level='error')


def repair_missing_sessions(raw, end_exclusive, get_quotes=None, calendar=None,
                            allow_trailing=False, now_utc=None, label='行情',
                            min_coverage=MIN_FILL_COVERAGE):
    """偵測 + 補值 + 回報。回傳 (新 raw, results)。

    行事曆不可用 → 警告並略過檢查；交易所失敗 → 該缺日標為 unfilled（由 gap_gate 決定是否中止）。
    """
    close = raw['Close']
    if close.empty:
        return raw, []
    try:
        if calendar is None:
            import exchange_calendars as xcals
            calendar = xcals.get_calendar('XTAI')
        sessions = candidate_sessions(close.index[0], end_exclusive, calendar, now_utc)
        gaps = missing_sessions(close.index, sessions)
    except Exception as e:
        alert(f'{label}: 無法取得 XTAI 行事曆，略過缺日檢查: {e}')
        return raw, []
    if not gaps:
        print(f'   ✅ {label} 缺日檢查：{len(sessions)} 個交易日全數都有資料')
        return raw, []
    last_bar = close.index[-1]
    n_trailing = sum(1 for g in gaps if g > last_bar)
    print(f'   🔎 {label} 缺日檢查：Yahoo 缺 {len(gaps)} 個 XTAI 交易日: '
          f'{", ".join(g.strftime("%Y-%m-%d") for g in gaps[-12:])}'
          + (f'（其中 {n_trailing} 個在最後一根 bar 之後'
             + ('，將以交易所行情補上）' if allow_trailing else '，交由新鮮度關卡處理）')
             if n_trailing else ''))
    # 最後一根 bar 之後的缺日：不補時交給新鮮度關卡，不計入上限、也不視為「中間缺日」
    attempt = gaps if allow_trailing else [g for g in gaps if g <= last_bar]
    if not attempt:
        return raw, []
    if len(attempt) > MAX_GAPS:
        results = [{'date': g.strftime('%Y-%m-%d'), 'status': 'unfilled', 'notes': [],
                    'reason': f'缺日數 {len(attempt)} 超過上限 {MAX_GAPS}，未嘗試補值'}
                   for g in attempt]
        alert(f'{label}: Yahoo 中間缺 {len(attempt)} 個交易日（超過上限 {MAX_GAPS}，未嘗試補值）: '
              f'{attempt[0]:%Y-%m-%d} … {attempt[-1]:%Y-%m-%d}。資料源或行事曆可能整體異常', level='error')
        return raw, results
    if get_quotes is None:
        from strategy.tw_quotes import fetch_day
        codes = list(close.columns)
        get_quotes = lambda d: fetch_day(d, codes)
    raw, results = fill_gaps(raw, attempt, get_quotes, allow_trailing=allow_trailing,
                             min_coverage=min_coverage)
    idx = raw['Close'].index
    report_gap_results(results, label, recent_cutoff=_recent_cutoff(idx))
    return raw, results


def _recent_cutoff(index, window=RECENT_WINDOW):
    if len(index) == 0:
        return None
    return index[-window] if len(index) >= window else index[0]


def blocking_gaps(results, index, window=RECENT_WINDOW):
    """會讓今日訊號算錯的缺日（落在最後 window 個交易日內）：
    - unfilled：整天沒補回；
    - filled 但有代號無法向交易所確認（上櫃行情取得失敗）：這些代號會被 ffill 成假的平盤日。
    （換算檢查未通過的個別代號只發警告：等同 Yahoo 個股缺值，沿用既有的容忍方式。）
    """
    cutoff = _recent_cutoff(index, window)
    out = []
    for r in results:
        bad = r['status'] == 'unfilled' or (r['status'] == 'filled' and r.get('missing'))
        if bad and (cutoff is None or pd.Timestamp(r['date']) >= cutoff):
            out.append(r['date'])
    return out


def gap_gate(results, index, allow=False, what='行情'):
    """中間缺日關卡：有 blocking 缺日 → 中止（exit 3），除非 allow。"""
    blocking = blocking_gaps(results, index)
    if not blocking:
        return []
    msg = (f"{what}中間缺少交易日 {', '.join(blocking)}，且無法從交易所完整補回；"
           f"今日的動能、均線、ATR 會被算錯。")
    if allow:
        alert(msg + " --allow-gaps 已指定，繼續執行。")
        return blocking
    alert(msg + " 中止，不產生訂單。稍後重跑通常即可補回；確認後可加 --allow-gaps 強制執行。",
          level='error')
    sys.exit(3)
