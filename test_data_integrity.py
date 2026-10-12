"""Regression tests for the 2026-09-30 / 2026-10-01 order incident.

事故：
1. '.TWO' 後綴被改寫成 '3529O' 等不存在代號。
2. 資料商給出空白的最新一列，Close 被 ffill 補值後偽裝成新交易日，
   ATR 算不出來又靜默改用固定 +15%/-8%。
3. 訂單出廠前、下游讀取時都沒有任何驗證，也沒有任何警訊。

全部離線執行：以假的 yf.download 取代網路。
"""
import json
import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import strategy.ai_strategy as S
import strategy.data_quality as DQ
from ai_report import validate_orders

IDX = pd.bdate_range('2026-08-03', '2026-10-01')
TICKERS = ['2330', '2454', '2303', '3529', '5274']
OTC = {'3529', '5274'}


def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _fake_download_factory(phantom_last=None, holiday=None, single_level=False):
    """phantom_last: None | 'nan' (整列 NaN) | 'zero_vol' (價格 NaN、Volume=0) | 'no_hl' (High/Low 缺)"""
    def fake_download(symbols, start=None, end=None, progress=False, **kw):
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        cols = pd.MultiIndex.from_product([['Close', 'Open', 'High', 'Low', 'Volume'], symbols])
        rng = np.random.default_rng(len(symbols))
        df = pd.DataFrame(rng.uniform(90, 110, (len(IDX), len(cols))), index=IDX, columns=cols)
        for sym in symbols:
            code, suffix = sym.split('.')
            if code in OTC and suffix == 'TW':     # 上櫃股用 .TW 抓不到
                df.loc[:, (slice(None), sym)] = np.nan
        for when, mode in ((IDX[-1], phantom_last), (holiday, 'nan')):
            if when is None or mode is None:
                continue
            if mode == 'nan':
                df.loc[when] = np.nan
            elif mode == 'no_hl':                   # 6/22、8/31 型：Close/Volume 有值、High/Low 缺
                for field, sym in df.columns:
                    if field in ('High', 'Low'):
                        df.loc[when, (field, sym)] = np.nan
            else:
                for field, sym in df.columns:
                    df.loc[when, (field, sym)] = 0.0 if field == 'Volume' else np.nan
        if single_level and len(symbols) == 1:
            df.columns = df.columns.droplevel(1)
        return df
    return fake_download


@pytest.fixture
def patch_yf(monkeypatch):
    def _apply(**kw):
        monkeypatch.setattr(S.yf, 'download', _fake_download_factory(**kw))
    return _apply


# ── #1 代號正規化 ────────────────────────────────────────────────
def test_normalize_tw_symbol_strips_only_trailing_suffix():
    assert DQ.normalize_tw_symbol('3529.TWO') == '3529'
    assert DQ.normalize_tw_symbol('2330.TW') == '2330'
    assert DQ.normalize_tw_symbol('00631L.TW') == '00631L'
    assert DQ.normalize_tw_symbol('2881A.TW') == '2881A'
    assert DQ.normalize_tw_symbol('2330') == '2330'


def test_valid_code_format():
    assert DQ.is_valid_tw_code('2330') and DQ.is_valid_tw_code('00631L')
    assert not DQ.is_valid_tw_code('Close') and not DQ.is_valid_tw_code('3529.TWO')


def test_otc_tickers_keep_their_real_code(patch_yf):
    patch_yf()
    close, *_ = S.fetch_panel_data(TICKERS, days=60, end_date='2026-10-02')
    assert set(close.columns) == set(TICKERS)
    assert not any(c.endswith('O') for c in close.columns)
    assert close['3529'].notna().all()          # .TWO 資料併回正確代號


# ── #5 下載結果與請求不符 ────────────────────────────────────────
def test_unrequested_symbol_raises(monkeypatch):
    base = _fake_download_factory()

    def leaky(symbols, **kw):
        df = base(symbols, **kw)
        extra = df.xs(df.columns.levels[1][0], level=1, axis=1).copy()
        extra.columns = pd.MultiIndex.from_product([extra.columns, ['9999.TW']])
        return pd.concat([df, extra], axis=1)

    monkeypatch.setattr(S.yf, 'download', leaky)
    with pytest.raises(ValueError, match='9999'):
        S.fetch_panel_data(['2330'], days=60, end_date='2026-10-02')


def test_single_level_columns_get_ticker_level(patch_yf):
    patch_yf(single_level=True)
    close, *_ = S.fetch_panel_data(['2330'], days=60, end_date='2026-10-02')
    assert list(close.columns) == ['2330']


def test_missing_everywhere_alerts(monkeypatch, capsys):
    base = _fake_download_factory()

    def no_8888(symbols, **kw):
        df = base(symbols, **kw)
        for sym in symbols:
            if sym.startswith('8888'):
                df.loc[:, (slice(None), sym)] = np.nan
        return df

    monkeypatch.setattr(S.yf, 'download', no_8888)
    S.fetch_panel_data(['2330', '8888'], days=60, end_date='2026-10-02')
    assert '8888' in capsys.readouterr().out


# ── #2 假 bar ────────────────────────────────────────────────────
@pytest.mark.parametrize('mode', ['nan', 'zero_vol', 'no_hl'])
def test_phantom_last_bar_is_dropped(patch_yf, mode):
    patch_yf(phantom_last=mode)
    close, open_, high, low, vol = S.fetch_panel_data(TICKERS, days=60, end_date='2026-10-02')
    assert close.index[-1] == IDX[-2]           # 假的 10/01 不得出現
    assert high.iloc[-1].notna().all()


def test_phantom_mid_history_is_dropped(patch_yf):
    patch_yf(holiday=IDX[10])
    close, *_ = S.fetch_panel_data(TICKERS, days=60, end_date='2026-10-02')
    assert IDX[10] not in close.index
    assert close.index[-1] == IDX[-1]


def test_partial_suspension_is_not_a_phantom_day():
    close = pd.DataFrame({'A': [1.0, 1.0], 'B': [1.0, np.nan], 'C': [1.0, 1.0]})
    vol = pd.DataFrame({'A': [5, 5], 'B': [5, np.nan], 'C': [5, 5]})
    keep, dropped = DQ.drop_phantom_bars(close, vol)
    assert dropped == [] and len(keep) == 2


# ── #7 #8 新鮮度與排程日期 ───────────────────────────────────────
@pytest.mark.parametrize('now, expected', [
    ('2026-10-01T16:32:00', '2026-10-01'),   # 事故當晚（台灣 10/02 00:32）
    ('2026-10-02T05:00:00', '2026-10-01'),   # 台灣 13:00，10/02 尚未收盤
    ('2026-10-02T09:17:00', '2026-10-02'),   # 排程時間，10/02 資料已就緒
    ('2026-10-03T09:17:00', '2026-10-02'),   # 週六
    ('2026-09-28T09:17:00', '2026-09-24'),   # 9/25 中秋、9/28 教師節
])
def test_expected_latest_session(now, expected):
    assert DQ.expected_latest_session(_utc(now)) == pd.Timestamp(expected)


def test_freshness_flags_the_1001_incident():
    fresh, expected = DQ.check_data_freshness(pd.Timestamp('2026-09-30'), now_utc=_utc('2026-10-01T16:32:00'))
    assert not fresh and expected == pd.Timestamp('2026-10-01')
    fresh, _ = DQ.check_data_freshness(pd.Timestamp('2026-10-01'), now_utc=_utc('2026-10-01T16:32:00'))
    assert fresh


@pytest.mark.parametrize('now, expected', [
    ('2026-10-02T09:17:00', date(2026, 10, 2)),   # 週五 17:17
    ('2026-10-02T16:30:00', date(2026, 10, 2)),   # 拖到週六 00:30，仍算週五
    ('2026-10-03T03:59:00', date(2026, 10, 2)),   # 週六 11:59
    ('2026-10-03T04:00:00', date(2026, 10, 3)),   # 週六 12:00
])
def test_report_reference_date(now, expected):
    assert DQ.report_reference_date(_utc(now)) == expected


# ── #16 CI annotation ───────────────────────────────────────────
def test_alert_emits_github_annotation(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    DQ.alert('line1\nline2 100%', level='error')
    out = capsys.readouterr().out
    assert '::error::line1%0Aline2 100%25' in out


def test_alert_plain_outside_ci(monkeypatch, capsys):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    DQ.alert('hello')
    assert '::' not in capsys.readouterr().out


# ── #4 #6 訂單關卡 ───────────────────────────────────────────────
def _order(t, ref=100.0, tp=110.0, sl=95.0):
    return {'ticker': t, 'reference_close': ref, 'tp_price': tp, 'sl_price': sl}


def test_validate_orders_rejects_unknown_ticker():
    with pytest.raises(RuntimeError, match='3529O'):
        validate_orders([_order('3529O')], TICKERS)


def test_validate_orders_rejects_bar_without_high_low():
    d = pd.Timestamp('2026-10-01')
    high = pd.DataFrame({'2330': [np.nan]}, index=[d])
    low = pd.DataFrame({'2330': [np.nan]}, index=[d])
    with pytest.raises(RuntimeError, match='High/Low'):
        validate_orders([_order('2330')], TICKERS, high, low, d)


@pytest.mark.parametrize('bad', [
    _order('2330', tp=90.0),             # 停利低於進場
    _order('2330', sl=np.nan),           # NaN
    _order('2330', sl=-1.0),             # 負數
])
def test_validate_orders_rejects_bad_prices(bad):
    with pytest.raises(RuntimeError, match='價格不合理'):
        validate_orders([bad], TICKERS)


def test_validate_orders_rejects_duplicates():
    with pytest.raises(RuntimeError, match='重複'):
        validate_orders([_order('2330'), _order('2330')], TICKERS)


def test_validate_orders_accepts_clean_orders():
    d = pd.Timestamp('2026-09-30')
    high = pd.DataFrame({'2330': [101.0], '3529': [3400.0]}, index=[d])
    low = pd.DataFrame({'2330': [99.0], '3529': [3200.0]}, index=[d])
    orders = [_order('2330'), _order('3529', 3305, 3800, 3000)]
    assert validate_orders(orders, TICKERS, high, low, d) == orders


def test_filter_orders_by_universe(capsys):
    kept = DQ.filter_orders_by_universe([_order('3529O'), _order('3529')], TICKERS)
    assert [o['ticker'] for o in kept] == ['3529']
    assert '3529O' in capsys.readouterr().out
    assert len(DQ.filter_orders_by_universe([_order('X')], None)) == 1   # 舊格式不過濾


# ── #10 下游消費者 ───────────────────────────────────────────────
ROW = ('<tr><td>{c}</td><td>3.90</td><td>{p}</td><td><span>{status}</span></td>'
       '<td><b>停利:</b> <span style="color:#00ff00">{tp}</span> (+15.0%) '
       '<br><b>停損:</b> <span style="color:#ff4444">{sl}</span> (-8.0%) '
       '<br><b>最晚出場:</b> 2026-11-09</td><td>x</td></tr>\n')
REJECTED = ('<tr><td>6505</td><td>4.00</td><td>97.3</td><td><span>⛔ 已剔除 #1</span></td>'
            '<td><span>⚠️ 訊號日無成交資料，不下單</span></td><td>x</td></tr>\n')
HTML = (REJECTED
        + ROW.format(c='3443', p='8040.0', status='🟢 建議買進 #2', tp='9246.0', sl='7396.8')
        + ROW.format(c='00631L', p='300.0', status='🟢 建議買進 #3', tp='345.0', sl='276.0'))


def test_paper_trade_regex_does_not_cross_rows():
    import paper_trade
    rows = paper_trade.extract_signal_rows(HTML)
    assert [r[0] for r in rows] == ['3443', '00631L']
    assert rows[0][1:4] == ('8040.0', '9246.0', '7396.8')     # 6505 不會配到 3443 的價格


def test_reconcile_flags_dropped_rows(capsys):
    dropped = DQ.reconcile_parsed_signals(HTML, ['3443'])
    assert dropped == ['00631L']
    assert '00631L' in capsys.readouterr().out


def test_paper_tracker_rejects_orders_outside_universe(tmp_path, monkeypatch, capsys):
    import paper_tracker
    monkeypatch.chdir(tmp_path)
    os.makedirs('artifacts')
    fname = f"artifacts/orders_{date.today().strftime('%Y%m%d')}.json"
    order = {'side': 'buy', 'limit_price': 100, 'tp_price': 110, 'sl_price': 95,
             'execution_date': None, 'max_hold_days': 20}
    payload = {'universe': TICKERS, 'orders': [dict(order, ticker='3529O'),
                                                dict(order, ticker='3529')]}
    json.dump(payload, open(fname, 'w'))
    sigs = paper_tracker.extract_signals_from_orders()
    assert [s['ticker'] for s in sigs] == ['3529']
    assert '3529O' in capsys.readouterr().out


def test_paper_tracker_html_fallback_by_column(tmp_path, monkeypatch):
    import paper_tracker
    monkeypatch.chdir(tmp_path)
    open('stock_report.html', 'w', encoding='utf-8').write(HTML)
    sigs = paper_tracker.extract_signals_from_report()
    got = {s['ticker']: s['entry'] for s in sigs}
    assert got == {'3443': 8040.0, '00631L': 300.0}


# ── #11 Yahoo 連結 ───────────────────────────────────────────────
def test_stock_link_without_meta_uses_bare_code(monkeypatch):
    import build_paper_page as B
    monkeypatch.setattr(B, '_stock_meta', lambda: {})
    assert "quote/3529'" in B._stock_link('3529')
    monkeypatch.setattr(B, '_stock_meta', lambda: {'3529': ('力旺', 'TPEx')})
    assert "quote/3529.TWO'" in B._stock_link('3529')
    assert 'href' not in B._stock_link('3529O.X')


# ── #12 警告不再被全域吞掉 ───────────────────────────────────────
def test_user_warnings_are_not_globally_ignored():
    import subprocess
    import sys
    import warnings
    # pytest 每個測試都會重設 warnings.filters，看不到 import 時的副作用 → 用乾淨的子程序檢查。
    # filterwarnings('ignore') 會留下 (action='ignore', message=None, category=Warning) 這筆。
    probe = (
        "import warnings, ai_report, paper_tracker, paper_trade, strategy.us_market;"
        "print(sum(1 for f in warnings.filters "
        "if f[0]=='ignore' and f[1] is None and f[2] is Warning))"
    )
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.abspath(__file__)))
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().splitlines()[-1] == '0'
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('default')
        DQ.quiet_routine_warnings()
        warnings.warn('data anomaly', UserWarning)
    assert any('data anomaly' in str(w.message) for w in caught)


# ── #8 #15 workflow 設定 ────────────────────────────────────────
def test_workflow_guards_are_wired():
    import yaml
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '.github', 'workflows', 'update_ai_report.yml')
    wf = yaml.safe_load(open(path, encoding='utf-8'))
    steps = wf['jobs']['generate-report']['steps']
    twcheck = next(s for s in steps if s.get('id') == 'twcheck')
    assert 'report_reference_date' in twcheck['run']
    assert 'utcnow' not in twcheck['run']
    report_steps = [s for s in steps if 'python ai_report.py' in s.get('run', '')]
    assert len(report_steps) == 4
    assert all('--allow-stale' in s['run'] for s in report_steps)
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact'))
    assert 'orders_*.json' in upload['with']['path']
    assert 'always()' in upload['if']


# ── 欄位順序（同分決勝依欄位順序，順序變了選股就變）────────────────
@pytest.mark.parametrize('vendor_sorts', [False, True])
def test_column_order_matches_live_order(monkeypatch, vendor_sorts):
    """上市股維持下載順序，.TWO 補抓的上櫃股在最後：與 5/26 以來上線行為一致。"""
    base = _fake_download_factory()

    def dl(symbols, **kw):
        df = base(symbols, **kw)
        return df.sort_index(axis=1, level=[0, 1]) if vendor_sorts else df

    monkeypatch.setattr(S.yf, 'download', dl)
    req = ['2454', '3529', '2330', '5274', '2303']
    close, *_ = S.fetch_panel_data(req, days=60, end_date='2026-10-02')
    listed = ['2303', '2330', '2454'] if vendor_sorts else ['2454', '2330', '2303']
    otc = ['3529', '5274']
    assert list(close.columns) == listed + otc


# ── HTML 後援路徑也要有股池關卡 ─────────────────────────────────
HTML_WITH_UNIVERSE = (
    '<head>' + DQ.universe_meta_tag(['3443', '3529', '6505']) + '</head>\n'
    + ROW.format(c='3529O', p='3305.0', status='🟢 建議買進 #1', tp='3800.7', sl='3040.6')
    + ROW.format(c='3443', p='8040.0', status='🟢 建議買進 #2', tp='9246.0', sl='7396.8')
)


def test_report_universe_roundtrip():
    assert DQ.report_universe(HTML_WITH_UNIVERSE) == ['3443', '3529', '6505']
    assert DQ.report_universe('<html></html>') is None


def test_paper_trade_html_rejects_ticker_outside_embedded_universe(capsys):
    import paper_trade
    rows = paper_trade.extract_signal_rows(HTML_WITH_UNIVERSE)
    assert [r[0] for r in rows] == ['3443']
    assert '3529O' in capsys.readouterr().out


def test_paper_tracker_html_rejects_ticker_outside_embedded_universe(tmp_path, monkeypatch, capsys):
    import paper_tracker
    monkeypatch.chdir(tmp_path)
    open('stock_report.html', 'w', encoding='utf-8').write(HTML_WITH_UNIVERSE)
    sigs = paper_tracker.extract_signals_from_report()
    assert [s['ticker'] for s in sigs] == ['3443']
    assert '3529O' in capsys.readouterr().out


# ── 重算訂單 workflow 的策略參數必須與正式 workflow 完全一致 ──────────
def _ai_report_args(run):
    import re
    import shlex
    cmd = run.split('python ai_report.py', 1)[1]
    lines = []
    for line in cmd.splitlines():
        lines.append(line)
        if not line.rstrip().endswith('\\'):
            break
    cmd = ' '.join(l.rstrip().rstrip('\\') for l in lines)
    cmd = re.sub(r'\$\{\{.*?\}\}', '', cmd)
    toks = shlex.split(cmd)
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if t == '--end-date':
            skip = True
            continue
        if t == '--allow-stale':
            continue
        out.append(t)
    return out


def test_regenerate_workflow_uses_same_strategy_args():
    import yaml
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.github', 'workflows')
    main = yaml.safe_load(open(os.path.join(base, 'update_ai_report.yml'), encoding='utf-8'))
    regen = yaml.safe_load(open(os.path.join(base, 'regenerate_orders.yml'), encoding='utf-8'))
    main_steps = {s['name']: s for s in main['jobs']['generate-report']['steps'] if 'name' in s}
    regen_steps = {s['name']: s for s in regen['jobs']['regenerate']['steps'] if 'name' in s}
    pairs = [('GUARD report (mom_guard)', 'GUARD (mom_guard)'),
             ('SURGE PRO report (mom_surge_pro) — 最後跑，paper 追蹤此策略', 'SURGE PRO (mom_surge_pro)')]
    for m, r in pairs:
        assert _ai_report_args(main_steps[m]['run']) == _ai_report_args(regen_steps[r]['run']), (m, r)
    assert '--end-date' in regen_steps['GUARD (mom_guard)']['run']
    assert regen['permissions'] == {'contents': 'read'}          # 不可 push
    assert 'git push' not in open(os.path.join(base, 'regenerate_orders.yml'), encoding='utf-8').read()


# ── 盤中執行：當日 bar 未收盤，不可使用（上線紀錄 2026-05-26 10:28 曾發生）────
@pytest.mark.parametrize('now, closed', [
    ('2026-05-26T02:28:00', False),   # 台灣 10:28 盤中
    ('2026-05-26T05:34:00', False),   # 13:34 尚未過緩衝
    ('2026-05-26T05:35:00', True),    # 13:35
    ('2026-05-26T09:17:00', True),    # 排程時間
])
def test_is_session_closed(now, closed):
    assert DQ.is_session_closed(pd.Timestamp('2026-05-26'), _utc(now)) is closed


# ── 依上線紀錄歸納的資料商異常型態（2026-04 ~ 10）──────────────────
def _panel(n=30, k=10):
    idx = pd.bdate_range('2026-08-03', periods=n)
    rng = np.random.default_rng(1)
    c = pd.DataFrame(100 + rng.normal(0, 1, (n, k)).cumsum(0), index=idx, columns=[f'{2300+i}' for i in range(k)])
    return c, c + 1, c - 1, c * 0 + 1e6


@pytest.mark.parametrize('pattern', ['blank', 'no_hl', 'flat_copy_with_volume', 'flat_copy_zero_volume'])
def test_live_incident_patterns_are_dropped(pattern):
    c, h, l, v = _panel()
    d = c.index[-1]
    if pattern == 'blank':                      # 10/01、7/02：整列空白
        for df in (c, h, l, v): df.loc[d] = np.nan
    elif pattern == 'no_hl':                    # 6/22、8/31：Close 有值、High/Low 缺
        h.loc[d] = np.nan; l.loc[d] = np.nan
    else:                                       # 7/10 颱風休市：複製前收的平盤 bar
        prev = c.iloc[-2]
        c.loc[d] = prev; h.loc[d] = prev; l.loc[d] = prev
        if pattern == 'flat_copy_zero_volume': v.loc[d] = 0
    keep, dropped = DQ.drop_phantom_bars(c, v, h, l)
    assert dropped == [d] and keep[-1] == c.index[-2]


def test_normal_panel_keeps_every_day():
    c, h, l, v = _panel()
    keep, dropped = DQ.drop_phantom_bars(c, v, h, l)
    assert dropped == [] and len(keep) == len(c)


# ── 漏跑警報（2026-10-08：排程延遲到台灣 10/09 00:33，被誤判為補假而跳過）──────
@pytest.mark.parametrize('last, ref, expected', [
    ('2026-10-07', '2026-10-09', ['2026-10-08']),   # 10/09 補假跳過，但 10/08 沒處理 → 警報
    ('2026-10-08', '2026-10-09', []),               # 10/08 已處理 → 正常跳過
    ('2026-10-07', '2026-10-12', ['2026-10-08']),   # 交易日照常執行，但中間漏了 10/08
    ('2026-10-08', '2026-10-12', []),               # 10/09–10/11 都是休市 → 無漏跑
    ('2026-10-07', '2026-10-08', []),               # 正常的下一個交易日
    ('2026-10-12', '2026-10-12', []),               # 同日重跑
])
def test_unprocessed_sessions(last, ref, expected):
    assert DQ.unprocessed_sessions(last, ref) == expected


def test_last_report_date():
    assert DQ.last_report_date('<p>報表日期: 2026-10-07 &nbsp;|</p>') == '2026-10-07'
    assert DQ.last_report_date('<html></html>') is None


def test_workflow_has_missed_run_alarm():
    import yaml
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '.github', 'workflows', 'update_ai_report.yml')
    steps = yaml.safe_load(open(path, encoding='utf-8'))['jobs']['generate-report']['steps']
    twcheck = next(s for s in steps if s.get('id') == 'twcheck')
    assert 'unprocessed_sessions' in twcheck['run'] and 'missed_on_skip' in twcheck['run']
    alarm = next(s for s in steps if s.get('name') == 'Missed-run alarm')
    assert 'missed_on_skip' in alarm['if'] and 'exit 1' in alarm['run']
