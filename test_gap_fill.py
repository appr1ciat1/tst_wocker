"""缺日偵測與交易所補值（strategy.gap_fill / strategy.tw_quotes）。全部離線。

模擬方式：
- 先產生「原始成交價」與除息事件，交易所行情 = 原始價 + 參考價（前收 − 股利）。
- Yahoo 還原價 = 原始價 × 之後所有除息事件的 (1 − 股利 / 除息前一日收盤)。
- 從 Yahoo 拿掉一天，補值結果必須與 Yahoo 原本那天的還原價一致。
"""
import numpy as np
import pandas as pd
import pytest

import strategy.ai_strategy as S
import strategy.gap_fill as GF
import strategy.tw_quotes as TQ

SESSIONS = list(pd.bdate_range('2026-09-01', '2026-10-13'))
CODES = ['2330', '2603', '5274', '0050']
# 除息：2603 剛好在缺日當天除息；2330 在缺日的下一個交易日除息；0050 在更早之前除息
GAP = pd.Timestamp('2026-10-05')
DIVS = {('2603', GAP): 3.0, ('2330', pd.Timestamp('2026-10-06')): 5.0,
        ('0050', pd.Timestamp('2026-09-15')): 1.2}


class FakeCal:
    def __init__(self, sessions):
        self.s = pd.DatetimeIndex(sessions)

    def sessions_in_range(self, a, b):
        return self.s[(self.s >= pd.Timestamp(a)) & (self.s <= pd.Timestamp(b))]


NOW = pd.Timestamp('2026-10-14 10:00')   # UTC；10/13 的資料已就緒


def _market():
    rng = np.random.default_rng(7)
    raw = {}
    for f in ['Open', 'High', 'Low', 'Close', 'Volume']:
        raw[f] = pd.DataFrame(index=pd.DatetimeIndex(SESSIONS), columns=CODES, dtype=float)
    refs = pd.DataFrame(index=pd.DatetimeIndex(SESSIONS), columns=CODES, dtype=float)
    for j, c in enumerate(CODES):
        prev = [500.0, 60.0, 120.0, 180.0][j]
        for d in SESSIONS:
            ref = prev - DIVS.get((c, d), 0.0)
            close = round(ref * (1 + rng.uniform(-0.05, 0.05)), 2)
            o = round(ref * (1 + rng.uniform(-0.02, 0.02)), 2)
            h = round(max(o, close) * (1 + rng.uniform(0, 0.02)), 2)
            lo = round(min(o, close) * (1 - rng.uniform(0, 0.02)), 2)
            for f, v in zip(['Open', 'High', 'Low', 'Close'], [o, h, lo, close]):
                raw[f].at[d, c] = v
            raw['Volume'].at[d, c] = float(rng.integers(1_000, 9_000_000))
            refs.at[d, c] = ref
            prev = close
    # Yahoo 還原價
    mult = pd.DataFrame(1.0, index=raw['Close'].index, columns=CODES)
    for (c, e), div in DIVS.items():
        prev_close = raw['Close'][c][raw['Close'].index < e].iloc[-1]
        mult.loc[mult.index < e, c] *= (1 - div / prev_close)
    yahoo = {f: (raw[f] * mult if f != 'Volume' else raw[f].copy()) for f in raw}
    return raw, refs, yahoo


def _quotes_fn(raw, refs, closed=(), absent=(), fail=(), calls=None, unchecked=()):
    def get(d):
        if calls is not None:
            calls.append(d)
        if d in fail:
            raise RuntimeError('TWSE timeout')
        if d in closed:
            return 'closed', pd.DataFrame(columns=TQ.QUOTE_COLUMNS), []
        rows = {c: {'open': raw['Open'].at[d, c], 'high': raw['High'].at[d, c],
                    'low': raw['Low'].at[d, c], 'close': raw['Close'].at[d, c],
                    'volume': raw['Volume'].at[d, c], 'ref': refs.at[d, c],
                    'source': 'TPEx' if c == '5274' else 'TWSE'}
                for c in CODES if c not in absent}
        df = pd.DataFrame.from_dict(rows, orient='index')
        df.attrs['unchecked'] = [c for c in unchecked if c in absent]
        return 'ok', df, []
    return get


def _drop(yahoo, dates):
    return {f: df.drop(index=list(dates)) for f, df in yahoo.items()}


def test_interior_gap_matches_yahoo_exactly_including_ex_dividend_days():
    raw, refs, yahoo = _market()
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs))
    assert [r['status'] for r in res] == ['filled']
    assert res[0]['n_filled'] == len(CODES) and res[0]['one_sided'] == 0
    for f in ['Open', 'High', 'Low', 'Close', 'Volume']:
        np.testing.assert_allclose(filled[f].loc[GAP].values, yahoo[f].loc[GAP].values, rtol=1e-9)
        assert list(filled[f].index) == list(yahoo[f].index)


def test_two_consecutive_gaps_are_chained():
    raw, refs, yahoo = _market()
    g2 = pd.Timestamp('2026-10-06')
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP, g2]), [GAP, g2], _quotes_fn(raw, refs))
    assert [r['status'] for r in res] == ['filled', 'filled']
    np.testing.assert_allclose(filled['Close'].loc[[GAP, g2]].values,
                               yahoo['Close'].loc[[GAP, g2]].values, rtol=1e-9)


def test_inconsistent_yahoo_next_day_is_rejected_per_ticker():
    raw, refs, yahoo = _market()
    bad = _drop(yahoo, [GAP])
    nxt = pd.Timestamp('2026-10-06')
    bad['Close'].at[nxt, '2603'] *= 1.03       # Yahoo 次日價格有誤
    filled, res = GF.fill_gaps(bad, [GAP], _quotes_fn(raw, refs))
    assert res[0]['status'] == 'filled'
    assert any(r.startswith('2603(') for r in res[0]['rejected'])
    assert np.isnan(filled['Close'].at[GAP, '2603'])
    assert filled['Close'].loc[GAP].notna().sum() == len(CODES) - 1


def test_exchange_closed_day_is_not_inserted():
    raw, refs, yahoo = _market()
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs, closed={GAP}))
    assert res[0]['status'] == 'closed'
    assert GAP not in filled['Close'].index


def test_network_failure_is_unfilled_not_closed():
    raw, refs, yahoo = _market()
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs, fail={GAP}))
    assert res[0]['status'] == 'unfilled'
    assert GAP not in filled['Close'].index
    assert GF.blocking_gaps(res, filled['Close'].index) == ['2026-10-05']


def test_old_unfilled_gap_is_not_blocking():
    idx = pd.bdate_range('2025-01-01', periods=400)
    res = [{'date': idx[10].strftime('%Y-%m-%d'), 'status': 'unfilled'}]
    assert GF.blocking_gaps(res, idx) == []


def test_otc_ticker_unchecked_is_reported_and_left_empty():
    raw, refs, yahoo = _market()
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP],
                               _quotes_fn(raw, refs, absent={'5274'}, unchecked={'5274'}))
    assert res[0]['status'] == 'filled' and res[0]['missing'] == ['5274'] and res[0]['no_trade'] == []
    assert np.isnan(filled['Close'].at[GAP, '5274'])


def test_suspended_ticker_is_no_trade_not_missing():
    raw, refs, yahoo = _market()
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs, absent={'2603'}))
    assert res[0]['status'] == 'filled' and res[0]['no_trade'] == ['2603'] and res[0]['missing'] == []
    assert np.isnan(filled['Close'].at[GAP, '2603'])


def test_single_series_suspension_is_no_trade():
    """0050 分割停牌（2025-06-11~17）：交易所有開市但 0050 無成交 → 不是缺漏，不可報錯。"""
    raw, refs, yahoo = _market()
    one = {'Close': _drop(yahoo, [GAP])['Close'][['0050']]}
    out, res = GF.fill_gaps(one, [GAP], _quotes_fn(raw, refs, absent={'0050'}), min_coverage=0.0)
    assert res[0]['status'] == 'no_trade' and GAP not in out['Close'].index
    assert GF.blocking_gaps(res, one['Close'].index) == []


def test_low_coverage_day_is_unfilled():
    raw, refs, yahoo = _market()
    _, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP],
                          _quotes_fn(raw, refs, absent={'2330', '2603', '5274'}))
    assert res[0]['status'] == 'unfilled'


def test_trailing_gap_only_with_flag_and_is_exact_one_sided():
    raw, refs, yahoo = _market()
    last = pd.Timestamp(SESSIONS[-1])
    cut = _drop(yahoo, [last])
    _, res = GF.fill_gaps(cut, [last], _quotes_fn(raw, refs))
    assert res[0]['status'] == 'skipped_trailing'
    filled, res = GF.fill_gaps(cut, [last], _quotes_fn(raw, refs), allow_trailing=True)
    assert res[0]['status'] == 'filled' and res[0]['one_sided'] == len(CODES)
    np.testing.assert_allclose(filled['Close'].loc[last].values, yahoo['Close'].loc[last].values,
                               rtol=1e-9)


def test_bad_reference_price_is_rejected_even_without_two_sided_check():
    raw, refs, yahoo = _market()
    last = pd.Timestamp(SESSIONS[-1])
    refs = refs.copy()
    refs.at[last, '2330'] = refs.at[last, '2330'] * 0.5    # 參考價解析錯誤 → 漲幅 >10%
    filled, res = GF.fill_gaps(_drop(yahoo, [last]), [last], _quotes_fn(raw, refs), allow_trailing=True)
    assert res[0]['rejected'] == ['2330(參考價不可用且無法反向換算)']
    assert np.isnan(filled['Close'].at[last, '2330'])


def test_candidate_sessions_respects_end_and_readiness():
    cal = FakeCal(SESSIONS)
    # end 是 exclusive（yfinance 慣例）
    got = GF.candidate_sessions('2026-10-01', '2026-10-06', cal, now_utc=NOW)
    assert [d.strftime('%m-%d') for d in got] == ['10-01', '10-02', '10-05']
    # 台灣 10/13 16:00（UTC 08:00）資料尚未就緒 → 不含 10/13
    got = GF.candidate_sessions('2026-10-12', '2026-10-14 09:17', cal,
                                now_utc=pd.Timestamp('2026-10-13 08:00'))
    assert [d.strftime('%m-%d') for d in got] == ['10-12']


def test_repair_end_to_end_with_fake_calendar():
    raw, refs, yahoo = _market()
    calls = []
    out, res = GF.repair_missing_sessions(_drop(yahoo, [GAP]), '2026-10-14', calendar=FakeCal(SESSIONS),
                                          get_quotes=_quotes_fn(raw, refs, calls=calls), now_utc=NOW)
    assert [r['status'] for r in res] == ['filled']
    assert calls == [GAP, pd.Timestamp('2026-10-06')]       # 缺日 + 反向檢查用的次日
    np.testing.assert_allclose(out['Close'].loc[GAP].values, yahoo['Close'].loc[GAP].values, rtol=1e-9)


def test_too_many_gaps_are_not_fetched(monkeypatch):
    raw, refs, yahoo = _market()
    monkeypatch.setattr(GF, 'MAX_GAPS', 2)
    calls = []
    gaps = [pd.Timestamp(d) for d in SESSIONS[5:8]]
    out, res = GF.repair_missing_sessions(_drop(yahoo, gaps), '2026-10-14', calendar=FakeCal(SESSIONS),
                                          get_quotes=_quotes_fn(raw, refs, calls=calls), now_utc=NOW)
    assert calls == [] and [r['status'] for r in res] == ['unfilled'] * 3


def test_fetch_panel_data_fills_vendor_gap(monkeypatch):
    """整條下載路徑：Yahoo 缺一天 → 補上，且補的那天不是 ffill 出來的假值。"""
    raw, refs, yahoo = _market()
    codes = ['2330', '2603']

    def fake_download(symbols, start=None, end=None, progress=False, **kw):
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        cols = pd.MultiIndex.from_product([['Close', 'Open', 'High', 'Low', 'Volume'], symbols])
        df = pd.DataFrame(index=yahoo['Close'].index.drop(GAP), columns=cols, dtype=float)
        for sym in symbols:
            c = sym.split('.')[0]
            if sym.endswith('.TW'):
                for f in ['Close', 'Open', 'High', 'Low', 'Volume']:
                    df[(f, sym)] = yahoo[f][c].drop(GAP).values
        return df

    import exchange_calendars as xcals
    monkeypatch.setattr(S.yf, 'download', fake_download)
    monkeypatch.setattr(xcals, 'get_calendar', lambda name: FakeCal(SESSIONS))
    monkeypatch.setattr(TQ, 'fetch_day', lambda d, c: _quotes_fn(raw, refs)(d))
    monkeypatch.setattr(GF, 'expected_latest_session', lambda **kw: pd.Timestamp(SESSIONS[-1]))
    rep = []
    close, open_, high, low, vol = S.fetch_panel_data(codes, start_date='2026-09-01', end_date='2026-10-14',
                                                      gap_report=rep)
    assert GAP in close.index
    np.testing.assert_allclose(close.loc[GAP].values, yahoo['Close'].loc[GAP, codes].values, rtol=1e-9)
    assert close.loc[GAP, '2330'] != close.loc[pd.Timestamp('2026-10-02'), '2330']
    assert [r['status'] for r in rep] == ['filled']
    assert not close.attrs        # 不可把結果放進 attrs（回測會因深拷貝變慢）


# ───────────── 交易所回應解析 ─────────────

TWSE_FIELDS = ['證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額', '開盤價', '最高價', '最低價',
               '收盤價', '漲跌(+/-)', '漲跌價差', '最後揭示買價', '最後揭示買量', '最後揭示賣價',
               '最後揭示賣量', '本益比']


def _twse_row(code, vol, o, h, l, c, sign, chg):
    return [code, 'x', vol, '1', '1', o, h, l, c, sign, chg, '', '', '', '', '']


def test_parse_twse_signs_and_missing_trades():
    payload = {'stat': 'OK', 'date': '20261012', 'tables': [
        {'title': '大盤', 'fields': ['指數', '收盤指數'], 'data': [['x', '1']]},
        {'fields': TWSE_FIELDS, 'data': [
            _twse_row('2330', '25,123,456', '1,450.00', '1,465.00', '1,440.00', '1,460.00',
                      '<p style= color:red>+</p>', '15.00'),
            _twse_row('2603', '9,000', '190.00', '191.00', '185.00', '186.50',
                      '<p style= color:green>-</p>', '3.50'),
            _twse_row('2303', '1,000', '50.00', '50.00', '50.00', '50.00', '<p> </p>', '0.00'),
            _twse_row('9999', '0', '--', '--', '--', '--', '<p> </p>', '0.00'),
            _twse_row('1101', '1,000', '30.00', '31.00', '29.00', '30.50', '<p> X</p>', '0.00'),
        ]}]}
    df = TQ.parse_twse(payload, '20261012')
    assert df.loc['2330', 'ref'] == 1445.0 and df.loc['2330', 'volume'] == 25123456
    assert df.loc['2603', 'ref'] == 190.0
    assert df.loc['2303', 'ref'] == 50.0
    assert '9999' not in df.index                 # 無成交
    assert np.isnan(df.loc['1101', 'ref'])        # 不比價 → 參考價未知


def test_parse_twse_holiday_vs_throttle():
    with pytest.raises(TQ.NoData):
        TQ.parse_twse({'stat': '很抱歉，沒有符合條件的資料!'}, '20261010')
    with pytest.raises(RuntimeError):
        TQ.parse_twse({'stat': '查詢過於頻繁，請稍後再試'}, '20261012')
    with pytest.raises(RuntimeError):
        TQ.parse_twse({'stat': 'OK', 'date': '20261008', 'tables': []}, '20261012')


def test_parse_tpex_new_and_legacy_formats():
    new = {'stat': 'ok', 'date': '20261012', 'tables': [{
        'fields': ['代號', '名稱', '收盤 ', '漲跌', '開盤 ', '最高 ', '最低', '均價 ', '成交股數  '],
        'data': [['5274', 'x', '3,500.00', '+50.00', '3,460.00', '3,520.00', '3,450.00', '1', '120,000']]}]}
    df = TQ.parse_tpex(new, '20261012')
    assert df.loc['5274', 'ref'] == 3450.0 and df.loc['5274', 'volume'] == 120000
    legacy = {'reportDate': '115/10/12', 'aaData': [
        ['3529', 'x', '800.00', '-10.00', '805.00', '812.00', '795.00', '1', '50,000']]}
    assert TQ.parse_tpex(legacy, '20261012').loc['3529', 'ref'] == 810.0
    with pytest.raises(TQ.NoData):          # TPEx 在休市日回傳前一交易日 → 必須拒收
        TQ.parse_tpex(dict(new, date='20261008'), '20261012')
    with pytest.raises(RuntimeError):       # 沒有日期 → 無法核對 → 不採用
        TQ.parse_tpex({'aaData': legacy['aaData']}, '20261012')


def test_fetch_day_uses_twse_as_authority_and_caches(monkeypatch, tmp_path):
    calls = []

    def fake_get(url, timeout=60):
        calls.append(url)
        if 'twse' in url:
            return {'stat': 'OK', 'date': '20261012', 'tables': [{'fields': TWSE_FIELDS, 'data': [
                _twse_row('2330', '1', '10', '11', '9', '10.5', '<p style= color:red>+</p>', '0.5')]}]}
        return {'stat': 'ok', 'date': '20261012', 'tables': [{
            'fields': ['代號', '名稱', '收盤', '漲跌', '開盤', '最高', '最低', '均價', '成交股數'],
            'data': [['5274', 'x', '100', '+1', '99', '101', '98', '1', '10']]}]}

    monkeypatch.setattr(TQ, '_get_json', fake_get)
    st, df, notes = TQ.fetch_day('2026-10-12', ['2330', '5274'])
    assert st == 'ok' and dict(df['source']) == {'2330': 'TWSE', '5274': 'TPEx'} and not notes
    n = len(calls)
    st2, df2, _ = TQ.fetch_day('2026-10-12', ['2330', '5274'])   # 第二次走快取
    assert len(calls) == n and st2 == 'ok'
    pd.testing.assert_frame_equal(df.drop(columns='source'), df2.drop(columns='source'),
                                  check_dtype=False)

    monkeypatch.setattr(TQ, '_get_json', lambda url, timeout=60: {'stat': '很抱歉，沒有符合條件的資料!'})
    assert TQ.fetch_day('2024-10-31', ['2330'])[0] == 'closed'


def test_gap_gate_exits_3_on_recent_unfilled_or_unchecked():
    idx = pd.bdate_range('2026-01-01', '2026-10-13')
    with pytest.raises(SystemExit) as e:
        GF.gap_gate([{'date': '2026-10-12', 'status': 'unfilled'}], idx)
    assert e.value.code == 3
    with pytest.raises(SystemExit):     # 補了但有上櫃股無法確認 → 這些股會被 ffill 成假平盤
        GF.gap_gate([{'date': '2026-10-12', 'status': 'filled', 'missing': ['5274']}], idx)
    assert GF.gap_gate([{'date': '2026-10-12', 'status': 'unfilled'}], idx, allow=True) == ['2026-10-12']
    assert GF.gap_gate([{'date': '2026-10-12', 'status': 'filled', 'missing': [],
                         'rejected': ['2330(x)']}], idx) == []
    assert GF.gap_gate([{'date': '2026-10-12', 'status': 'closed'}], idx) == []


def test_ai_report_wires_gap_gate_for_panel_and_regime_benchmark():
    src = open('ai_report.py', encoding='utf-8').read()
    assert src.count('gap_gate(') == 2 and 'gap_report=gap_results' in src and 'gap_report=bench_gaps' in src
    assert "'--allow-gaps'" in src and "'--fill-latest-from-exchange'" in src


def test_fetch_day_tpex_failure_marks_unchecked(monkeypatch):
    def fake_get(url, timeout=60):
        if 'twse' in url:
            return {'stat': 'OK', 'date': '20261012', 'tables': [{'fields': TWSE_FIELDS, 'data': [
                _twse_row('2330', '1', '10', '11', '9', '10.5', '<p style= color:red>+</p>', '0.5')]}]}
        raise RuntimeError('TPEx blocked')

    monkeypatch.setattr(TQ, '_get_json', fake_get)
    st, df, notes = TQ.fetch_day('2026-10-12', ['2330', '5274'])
    assert st == 'ok' and list(df.index) == ['2330']
    assert df.attrs['unchecked'] == ['5274'] and notes


def test_parse_twse_ok_without_stock_table_is_not_a_holiday():
    with pytest.raises(RuntimeError):
        TQ.parse_twse({'stat': 'OK', 'date': '20261012', 'tables': [{'fields': ['指數'], 'data': [['1']]}]},
                      '20261012')


def test_fetch_day_tpex_nodata_on_open_day_is_unchecked(monkeypatch):
    def fake_get(url, timeout=60):
        if 'twse' in url:
            return {'stat': 'OK', 'date': '20261012', 'tables': [{'fields': TWSE_FIELDS, 'data': [
                _twse_row('2330', '1', '10', '11', '9', '10.5', '<p style= color:red>+</p>', '0.5')]}]}
        return {'stat': 'ok', 'date': '20261009', 'tables': []}    # TPEx 回了別天

    monkeypatch.setattr(TQ, '_get_json', fake_get)
    st, df, notes = TQ.fetch_day('2026-10-12', ['2330', '5274'])
    assert df.attrs['unchecked'] == ['5274'] and notes


def test_chained_gaps_bad_first_day_is_caught_by_chained_backward_check():
    raw, refs, yahoo = _market()
    g2 = pd.Timestamp('2026-10-06')
    refs = refs.copy()
    refs.at[GAP, '2330'] *= 1.04          # g1 的參考價錯 4%（仍在漲跌幅內）
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP, g2]), [GAP, g2], _quotes_fn(raw, refs))
    assert any(r.startswith('2330(前後換算不一致') for r in res[0]['rejected'])
    assert res[0]['one_sided'] == 0 and res[1]['one_sided'] == 0
    assert np.isnan(filled['Close'].at[GAP, '2330'])


def test_unknown_reference_price_uses_backward_factor():
    """除權息日交易所可能標 X（不比價）→ 參考價未知；有次日行情時改用反向換算，結果仍精確。"""
    raw, refs, yahoo = _market()
    refs = refs.copy()
    refs.at[GAP, '2603'] = np.nan         # 2603 剛好在 GAP 除息
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs))
    assert res[0]['rejected'] == [] and res[0]['one_sided'] == 1
    np.testing.assert_allclose(filled['Close'].at[GAP, '2603'], yahoo['Close'].at[GAP, '2603'], rtol=1e-9)


def test_next_day_fetch_failure_degrades_to_one_sided_with_note():
    raw, refs, yahoo = _market()
    nxt = pd.Timestamp('2026-10-06')
    filled, res = GF.fill_gaps(_drop(yahoo, [GAP]), [GAP], _quotes_fn(raw, refs, fail={nxt}))
    assert res[0]['status'] == 'filled' and res[0]['one_sided'] == len(CODES)
    assert any('只能單向換算' in n for n in res[0]['notes'])


def test_circuit_breaker_stops_hammering_a_blocked_host(monkeypatch, tmp_path):
    import urllib.error
    calls = []

    def boom(req, context=None, timeout=60):
        calls.append(req.full_url)
        raise urllib.error.URLError('blocked')

    monkeypatch.setattr(TQ.urllib.request, 'urlopen', boom)
    monkeypatch.setattr(TQ.time, 'sleep', lambda s: None)
    monkeypatch.setattr(TQ, '_fail_streak', {})
    for _ in range(TQ.BREAKER_THRESHOLD):
        with pytest.raises(urllib.error.URLError):
            TQ._http_get_json(TQ.TWSE_URL.format(ymd='20261012'))
    n = len(calls)
    with pytest.raises(RuntimeError, match='斷路器'):
        TQ._http_get_json(TQ.TWSE_URL.format(ymd='20261013'))
    assert len(calls) == n
    monkeypatch.setattr(TQ, '_fail_streak', {})       # 新的 process：靠快取目錄裡的標記
    with pytest.raises(RuntimeError, match='斷路器'):
        TQ._http_get_json(TQ.TWSE_URL.format(ymd='20261013'))
    # 別的主機不受影響
    with pytest.raises(urllib.error.URLError):
        TQ._http_get_json(TQ.TPEX_URLS[0].format(y=2026, m='10', d='12', roc=115))
