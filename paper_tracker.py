#!/usr/bin/env python3
"""
Paper Trading 自動追蹤器 v8.5

每日收盤後執行，自動模擬 v9 Hybrid Tiered (Core-Satellite + Vol Target) 策略的實盤績效：
1. 從 stock_report.html 擷取今日信號
2. 追蹤已持倉的 TP/SL/時間到期
3. 累積權益曲線到 paper_equity.json
4. 產出 paper_trading.html 績效網頁

使用方式:
  python paper_tracker.py              # 每日更新（GitHub Actions 自動執行）
  python paper_tracker.py --reset      # 清除所有記錄重新開始
  python paper_tracker.py --replay-from 2026-04-22  # 依最新回測從頭重播 paper
"""

import json
import glob
import os
import re
import sys
from datetime import datetime, date, timedelta
import argparse
from typing import Optional, Tuple

import pandas as pd

# v9 Hybrid Tiered support
from strategy.portfolio_vol_target import (
    PortfolioVolatilityTarget, TARGET_ANN_VOL_DEFAULT,
    COOLING_DAYS_DEFAULT, SAT_ALPHA_TRIM_FRAC_DEFAULT,
    SAT_ALPHA_TRIM_MIN_PNL_DEFAULT, CORE_ALPHA_TRIM_FRAC_DEFAULT,
    CORE_ALPHA_TRIM_MIN_PNL_DEFAULT, v3_vol_target_config,
)
from strategy.risk_metrics import compute_tiered_risk_summary, format_tiered_risk_summary
from strategy.core_holdings import CoreHoldingsManager

DATA_FILE = 'paper_equity.json'
HTML_FILE = 'paper_trading.html'


# ===================== 台股交易日曆（資料新鮮度 / 持有天數用） =====================
_TW_CAL = None


def _tw_calendar():
    """取得台股交易日曆（XTAI）。失敗時回 None，呼叫端退化為平日判斷。"""
    global _TW_CAL
    if _TW_CAL is None:
        try:
            import exchange_calendars as xcals
            _TW_CAL = xcals.get_calendar("XTAI")
        except Exception:
            _TW_CAL = False  # 標記為不可用
    return _TW_CAL or None


def is_trading_day(day: str) -> bool:
    """day (YYYY-MM-DD) 是否為台股交易日。無日曆時退化為週一～週五。"""
    cal = _tw_calendar()
    ts = pd.Timestamp(day)
    if cal is not None:
        try:
            return bool(cal.is_session(ts.normalize()))
        except Exception:
            pass
    return ts.weekday() < 5


def trading_days_held(entry_date: str, current_day: str) -> int:
    """entry_date 到 current_day（含）之間的台股交易日數（持有天數），對缺跑日穩健。"""
    cal = _tw_calendar()
    start, end = pd.Timestamp(entry_date), pd.Timestamp(current_day)
    if end < start:
        return 0
    if cal is not None:
        try:
            sessions = cal.sessions_in_range(start.normalize(), end.normalize())
            return max(len(sessions), 1)
        except Exception:
            pass
    return max(len(pd.bdate_range(start, end)), 1)


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        # v9 backward fill: ensure dual book keys exist for old paper_equity.json
        data.setdefault('core_equity_curve', [])
        data.setdefault('sat_equity_curve', [])
        data.setdefault('last_tiered', {})
        data.setdefault('cooling_days_left', 0)
        for pos in data.get('positions', {}).values():
            if 'book' not in pos:
                pos['book'] = 'satellite'
        return data
    return {
        'start_date': date.today().isoformat(),
        'initial_capital': 200_000,
        'capital': 200_000,
        'positions': {},          # {ticker: {entry, tp, sl, entry_date, shares, day_count, book: 'core'|'satellite'}}
        'closed_trades': [],      # 增加 'book' 欄位
        'equity_curve': [],       # 合併
        'core_equity_curve': [],
        'sat_equity_curve': [],
        'last_tiered': {},        # 最近一次 tiered scale 結果
        'last_fvol': None,        # 前日預測波動（資金輪動用）
        'cooling_days_left': 0,   # 波動回落後 Satellite 加碼視窗
        'daily_signals': [],      # [{date, tickers: [...]}]
    }

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

def get_current_bars(tickers):
    """用 yfinance 取得最新 OHLC，用於 paper fills 與 mark-to-market。"""
    import yfinance as yf
    bars = {}
    if not tickers:
        return bars

    def download(symbols):
        try:
            return yf.download(
                symbols, period='5d', progress=False, auto_adjust=False, threads=True,
            )
        except Exception:
            return None

    try:
        def read_bars(df, symbol_map):
            if df is None or df.empty:
                return {}
            parsed = {}
            for ticker, symbol in symbol_map.items():
                bar = {
                    'open': field_value(df, 'Open', symbol),
                    'high': field_value(df, 'High', symbol),
                    'low': field_value(df, 'Low', symbol),
                    'close': field_value(df, 'Close', symbol),
                }
                if bar['close'] is not None:
                    parsed[ticker] = bar
            return parsed

        def field_value(df, field, symbol):
            if isinstance(df.columns, pd.MultiIndex):
                if (field, symbol) not in df.columns:
                    return None
                series = df[(field, symbol)].dropna()
            elif field in df.columns:
                series = df[field].dropna()
            else:
                return None
            if len(series) == 0:
                return None
            return float(series.iloc[-1])

        tw_symbols = {t: f"{t}.TW" for t in tickers}
        bars.update(read_bars(download(list(tw_symbols.values())), tw_symbols))
        missing = [t for t in tickers if t not in bars]
        if missing:
            two_symbols = {t: f"{t}.TWO" for t in missing}
            bars.update(read_bars(download(list(two_symbols.values())), two_symbols))
        still_missing = [t for t in tickers if t not in bars]
        if still_missing:
            print(f"   ⚠️ 無法取得報價: {', '.join(still_missing)}")
    except Exception as e:
        print(f"   ⚠️ 價格下載失敗: {e}")
    return bars


def get_current_prices(tickers):
    """Backward-compatible latest close lookup."""
    return {ticker: bar['close'] for ticker, bar in get_current_bars(tickers).items()}


# ===================== v9 Core / Satellite helpers =====================

_CORE_TICKERS_CACHE = None

def _get_core_tickers() -> set:
    """取得當前 Core 持股清單（優先使用 manager 預設 + 靜態擴充）。"""
    global _CORE_TICKERS_CACHE
    if _CORE_TICKERS_CACHE is not None:
        return _CORE_TICKERS_CACHE
    try:
        mgr = CoreHoldingsManager(core_cap=5)
        # 無資料時至少返回結構性龍頭
        cores, _, _ = mgr.select_core(pd.DataFrame(), pd.DataFrame())  # 將由 caller 傳真實資料改善
    except Exception:
        cores = []
    static = {'2330', '2454', '2308', '2317'}  # 最低底線結構龍頭
    _CORE_TICKERS_CACHE = set(cores) | static
    return _CORE_TICKERS_CACHE


def classify_book(ticker: str, forced: Optional[str] = None) -> str:
    """判斷 ticker 屬於 core 還是 satellite book。"""
    if forced in ('core', 'satellite'):
        return forced
    # v9: 強制將經典結構性龍頭視為 Core（即使 manager 暫無足夠資料）
    CORE_ANCHORS = {'2330', '2454', '2308', '2317', '3008'}
    if ticker in CORE_ANCHORS:
        return 'core'
    cores = _get_core_tickers()
    return 'core' if ticker in cores else 'satellite'


def compute_split_equity(data: dict, prices: dict, initial_capital: float) -> Tuple[float, float, float]:
    """
    依 book 分別計算 core / sat / merged equity。
    返回 (core_eq, sat_eq, total_eq)
    """
    core_capital = 0.0
    sat_capital = 0.0
    core_mtm = 0.0
    sat_mtm = 0.0

    # 簡化模型：cash 目前不分拆，開新倉時按 book 比例扣；這裡用動態分拆持倉 MTM + 剩餘 cash 按比例
    # 實際 tracker 每次更新後 capital 是全現金；我們用 positions book 歸屬分 MTM
    for ticker, pos in data.get('positions', {}).items():
        book = pos.get('book', classify_book(ticker))
        price = prices.get(ticker, pos.get('entry', 0))
        mtm = price * pos.get('shares', 0)
        if book == 'core':
            core_mtm += mtm
        else:
            sat_mtm += mtm

    # 現金按最後部位數比例粗分（首次或空倉時全給 sat）
    total_pos = len(data.get('positions', {}))
    n_core = sum(1 for p in data.get('positions', {}).values() if p.get('book', 'satellite') == 'core')
    n_sat = total_pos - n_core

    cash = data.get('capital', 0)
    if total_pos == 0:
        sat_capital = cash
    else:
        core_capital = cash * (n_core / total_pos) if total_pos > 0 else 0
        sat_capital = cash - core_capital

    core_eq = core_capital + core_mtm
    sat_eq = sat_capital + sat_mtm
    total_eq = cash + sum(prices.get(t, data['positions'][t]['entry']) * data['positions'][t]['shares']
                          for t in data.get('positions', {}))
    return round(core_eq, 0), round(sat_eq, 0), round(total_eq, 0)


# 訂單超過此天數視為過期，當日不再依此開新倉（避免用數週前的訊號在今天進場）。
SIGNAL_MAX_AGE_DAYS = 5


def _order_file_date(path):
    m = re.search(r'orders_(\d{8})\.json', os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y%m%d').date()
    except ValueError:
        return None


def extract_signals_from_orders(run_day=None):
    """
    從 artifacts/orders_YYYYMMDD.json 擷取機器可讀訂單。

    重點修正：
    - 掃描「最近 SIGNAL_MAX_AGE_DAYS 天內」的所有訂單檔（不再只看最新一檔），
      避免 t+1 訂單（今天產生、明天執行）永遠不被消化。
    - 每檔股票優先選 execution_date == run_day 的訂單（即「昨日訊號、今日執行」），
      其次取較新檔。實際是否進場由 _open_positions 依 execution_date == day 決定。
    - 全部訂單檔都過期（> SIGNAL_MAX_AGE_DAYS）時，今日不開新倉並警告。
    """
    order_files = glob.glob('artifacts/orders_*.json')
    if not order_files:
        return []

    today = date.today()
    fresh_files = []
    for f in order_files:
        fd = _order_file_date(f)
        if fd is None or (today - fd).days <= SIGNAL_MAX_AGE_DAYS:
            fresh_files.append(f)

    if not fresh_files:
        latest = max(order_files, key=os.path.getmtime)
        age = (today - (_order_file_date(latest) or today)).days
        print(f"   ⚠️ 最新訂單 {latest} 已過期 {age} 天 (>{SIGNAL_MAX_AGE_DAYS})，"
              f"今日不依此開新倉。請先執行 ai_report.py 產生當日訊號。")
        return []

    merged = {}  # ticker -> order（優先 execution_date == run_day，其次較新檔）
    for f in sorted(fresh_files):  # 舊→新
        try:
            with open(f, encoding='utf-8') as fh:
                payload = json.load(fh)
        except Exception as e:
            print(f"   ⚠️ orders JSON 讀取失敗 {f}: {e}")
            continue
        for order in payload.get('orders', []):
            if order.get('side') != 'buy':
                continue
            t = order['ticker']
            prev = merged.get(t)
            if prev is None:
                merged[t] = order
                continue
            cand_match = run_day is not None and order.get('execution_date') == run_day
            prev_match = run_day is not None and prev.get('execution_date') == run_day
            if cand_match or (cand_match == prev_match):  # 偏好當日執行；否則較新檔覆蓋
                merged[t] = order

    signals = []
    for order in merged.values():
        signals.append({
            'ticker': order['ticker'],
            'entry': float(order.get('limit_price') or order.get('reference_close')),
            'tp': float(order['tp_price']),
            'sl': float(order['sl_price']),
            'execution_date': order.get('execution_date'),
            'max_hold_days': int(order.get('max_hold_days', 20)),
            'time_exit': order.get('time_exit'),
        })
    if signals:
        n_today = sum(1 for s in signals if s.get('execution_date') == run_day)
        print(f"   📦 訂單來源: {len(fresh_files)} 檔(新鮮), 候選 {len(signals)} 筆, "
              f"今日可執行 {n_today} 筆")
    return signals

def extract_signals_from_report(run_day=None):
    """從 orders JSON（優先）或 stock_report.html 擷取今日買入信號。"""
    order_signals = extract_signals_from_orders(run_day=run_day)
    if order_signals:
        return order_signals

    report_path = 'stock_report.html'
    if not os.path.exists(report_path):
        return []

    with open(report_path) as f:
        html = f.read()

    # Format: <td>TICKER</td><td>SCORE</td><td>ENTRY</td><td>...建議買進...</td>
    #         <td>停利: TP ... 停損: SL ...</td>
    signals = []
    rows = re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        if '建議買進' not in row:
            continue
        ticker_m = re.search(r'<td>(\d{4})</td>', row)
        entry_m = re.findall(r'<td[^>]*>([\d\.]+)</td>', row)
        tp_m = re.search(r'停利.*?>([\d\.]+)<', row)
        sl_m = re.search(r'停損.*?>([\d\.]+)<', row)
        if ticker_m and len(entry_m) >= 3 and tp_m and sl_m:
            signals.append({
                'ticker': ticker_m.group(1),
                'score': float(entry_m[1]),
                'entry': float(entry_m[2]),  # third number is entry price (1st=ticker, 2nd=score, 3rd=price)
                'tp': float(tp_m.group(1)),
                'sl': float(sl_m.group(1)),
                'max_hold_days': 20,
            })
    return signals

def _latest_market_date(reference='0050'):
    """回傳 yfinance 最新可得的交易日（用參考標的探測），失敗回 None。"""
    try:
        import yfinance as yf
        df = yf.download(f'{reference}.TW', period='7d', progress=False,
                         auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        return df.index[-1].strftime('%Y-%m-%d')
    except Exception:
        return None


def update_tracker(data):
    """主要更新邏輯：追蹤持倉、結算已平倉、記錄新信號。"""
    today = date.today().isoformat()
    reserve_cash = data['initial_capital'] * RESERVE_RATIO

    print(f"📊 Paper Tracker 更新 ({today})")
    print(f"   初始資金: {data['initial_capital']:,.0f}")
    print(f"   當前現金: {data['capital']:,.0f}")
    print(f"   保留現金: {reserve_cash:,.0f} ({RESERVE_RATIO:.0%} 本金)")
    print(f"   持倉檔數: {len(data['positions'])}")

    # 防呆 1：非交易日不更新（週末 / 台股休市日）
    if not is_trading_day(today):
        print(f"   ⏸️ {today} 非台股交易日，跳過更新")
        return

    # 防呆 2：同日只更新一次（避免重複計入）
    if data['equity_curve'] and data['equity_curve'][-1].get('date') == today:
        print(f"   ⚠️ 今日已更新過，跳過")
        return

    # 防呆 3：行情資料新鮮度 —— 今日資料尚未產生就不要用昨日 bar 當今日記錄
    market_date = _latest_market_date()
    if market_date is not None and market_date < today:
        print(f"   ⏸️ 行情最新僅到 {market_date}（今日 {today} 資料未就緒），跳過更新避免記錄過期資料")
        return

    all_tickers = list(data['positions'].keys())
    signals = extract_signals_from_report(run_day=today)
    signal_tickers = [s['ticker'] for s in signals]
    bars = get_current_bars(list(set(all_tickers + signal_tickers)))
    _process_trading_day(data, today, bars, signals, verbose=True)


BUY_COST_RATE = 0.001425
SELL_COST_RATE = 0.004425
SLIPPAGE = 0.001
MAX_HOLD_DAYS = 20
RESERVE_RATIO = 0.10
TOP_K = 7
COOLING_CORE_TRIM_FRAC = 0.20
COOLING_CORE_TRIM_MIN_PNL_PCT = 4.0


def _empty_tiered():
    return {
        "overall": 1.0, "core_trade_scale": 1.0, "sat_trade_scale": 1.0,
        "core_rotation_boost": 1.0, "sat_rotation_boost": 1.0,
        "vol_regime": 'normal', "rotate_sat_profits_to_core": 0.0,
        "rotate_core_profits_to_sat": 0.0, "sat_entry_freeze": 0.0,
    }


def _make_v3_pvt(data: dict) -> PortfolioVolatilityTarget:
    pvt = PortfolioVolatilityTarget(v3_vol_target_config())
    pvt._prev_forecast_vol = data.get('last_fvol')
    pvt._cooling_days_left = data.get('cooling_days_left', 0)
    return pvt


def _equity_series(data: dict, key: str) -> pd.Series:
    return pd.Series([p['equity'] for p in data.get(key, [])[-60:]])


def _vol_forecast_inputs(data: dict, day: Optional[str] = None) -> tuple:
    """Paper live uses simulated curves; replay prefers backtest equity for vol alignment."""
    bt_df = data.get('_bt_equity_df')
    if bt_df is not None and day:
        try:
            sub = bt_df[bt_df['Date'] < pd.Timestamp(day)].tail(60)
            if len(sub) >= 5:
                return None, None, pd.Series(sub['Equity'].astype(float).values)
        except Exception:
            pass
    return (
        _equity_series(data, 'core_equity_curve'),
        _equity_series(data, 'sat_equity_curve'),
        _equity_series(data, 'equity_curve'),
    )


def _compute_tiered_for_sizing(data: dict, day: Optional[str] = None) -> dict:
    tiered = _empty_tiered()
    try:
        pvt = _make_v3_pvt(data)
        ec, es, em = _vol_forecast_inputs(data, day)
        fvol = pvt.forecast_portfolio_ann_vol(ec, es, em)
        tiered = pvt.tiered_scale_factors(fvol)
        if tiered.get('cooling_transition', 0) >= 1.0:
            data['cooling_days_left'] = COOLING_DAYS_DEFAULT
        elif data.get('cooling_days_left', 0) > 0:
            data['cooling_days_left'] -= 1
        data['last_fvol'] = fvol
    except Exception:
        pass
    return tiered


def _trim_book_for_rotation(data, bars, book, trim_frac, min_pnl_pct, label, emoji, verbose=True):
    for ticker, pos in list(data['positions'].items()):
        if pos.get('book', classify_book(ticker)) != book:
            continue
        bar = bars.get(ticker)
        if not bar or bar.get('close') is None:
            continue
        pnl_pct = (bar['close'] / pos['entry'] - 1) * 100
        if pnl_pct < min_pnl_pct:
            continue
        shares_sell = int(pos['shares'] * trim_frac)
        if shares_sell <= 0:
            continue
        exit_price = bar['close']
        sell_cost = exit_price * shares_sell * SELL_COST_RATE
        slippage_cost = exit_price * shares_sell * SLIPPAGE
        proceeds = exit_price * shares_sell - sell_cost - slippage_cost
        data['capital'] += proceeds
        pos['shares'] -= shares_sell
        if verbose:
            print(f"   {emoji} {label} {ticker}: 減碼 {shares_sell:,.0f} 股 ({pnl_pct:+.1f}%)")


def _apply_rotation_trims(data, bars, tiered_for_sizing, verbose=True):
    if tiered_for_sizing.get('rotate_sat_profits_to_core', 0) >= 1.0:
        _trim_book_for_rotation(
            data, bars, 'satellite',
            SAT_ALPHA_TRIM_FRAC_DEFAULT,
            SAT_ALPHA_TRIM_MIN_PNL_DEFAULT * 100,
            '輪動至Core(波動升破)', '🟠', verbose,
        )
    if tiered_for_sizing.get('rotate_core_profits_to_sat', 0) >= 1.0:
        _trim_book_for_rotation(
            data, bars, 'core',
            CORE_ALPHA_TRIM_FRAC_DEFAULT,
            CORE_ALPHA_TRIM_MIN_PNL_DEFAULT * 100,
            '輪動回Sat(波動回落)', '🔵', verbose,
        )
    elif data.get('cooling_days_left', 0) > 0:
        _trim_book_for_rotation(
            data, bars, 'core',
            COOLING_CORE_TRIM_FRAC, COOLING_CORE_TRIM_MIN_PNL_PCT,
            '輪動回Sat(冷卻續跑)', '🔵', verbose,
        )


def _close_positions(data, day, bars, verbose=True):
    to_close = []
    for ticker, pos in data['positions'].items():
        # 以實際交易日數計算持有天數（對缺跑日穩健；非單純每次 +1）
        entry_d = pos.get('entry_date')
        if entry_d:
            pos['day_count'] = max(trading_days_held(entry_d, day) - 1, 0)
        else:
            pos['day_count'] = pos.get('day_count', 0) + 1
        bar = bars.get(ticker)
        if bar is None or bar.get('close') is None:
            continue

        reason = None
        exit_price = bar['close']
        pos_max_hold = pos.get('max_hold_days', MAX_HOLD_DAYS)
        if bar.get('low') is not None and bar['low'] <= pos['sl']:
            reason = 'SL'
            open_price = bar.get('open')
            exit_price = open_price if open_price is not None and open_price < pos['sl'] else pos['sl']
        elif bar.get('high') is not None and bar['high'] >= pos['tp']:
            reason = 'TP'
            open_price = bar.get('open')
            exit_price = open_price if open_price is not None and open_price > pos['tp'] else pos['tp']
        elif pos['day_count'] >= pos_max_hold:
            reason = 'TIME'
            exit_price = bar['close']

        if reason:
            sell_cost = exit_price * pos['shares'] * SELL_COST_RATE
            slippage_cost = exit_price * pos['shares'] * SLIPPAGE
            proceeds = exit_price * pos['shares'] - sell_cost - slippage_cost
            cost_basis = pos['entry'] * pos['shares'] * (1 + BUY_COST_RATE + SLIPPAGE)
            pnl = proceeds - cost_basis
            pnl_pct = (exit_price / pos['entry'] - 1) * 100
            data['capital'] += proceeds
            book = pos.get('book', classify_book(ticker))
            data['closed_trades'].append({
                'ticker': ticker,
                'entry': pos['entry'],
                'exit': exit_price,
                'shares': pos['shares'],
                'pnl': round(pnl, 0),
                'pnl_pct': round(pnl_pct, 2),
                'reason': reason,
                'entry_date': pos['entry_date'],
                'exit_date': day,
                'days_held': pos['day_count'],
                'book': book,
            })
            to_close.append(ticker)
            if verbose:
                emoji = '🟢' if pnl > 0 else '🔴'
                print(
                    f"   {emoji} 平倉 {ticker}: {pos['entry']:.1f}→{exit_price:.1f} "
                    f"({pnl_pct:+.1f}%) [{reason}] 持{pos['day_count']}天"
                )

    for ticker in to_close:
        del data['positions'][ticker]
    return to_close


def _open_positions(data, day, bars, signals, tiered_for_sizing, verbose=True):
    if not signals:
        if verbose:
            print(f"   📋 今日無信號")
        return 0

    signal_tickers = [s['ticker'] for s in signals]
    data['daily_signals'].append({'date': day, 'tickers': signal_tickers})
    max_new = TOP_K - len(data['positions'])
    candidates = []
    reserve_cash = data['initial_capital'] * RESERVE_RATIO

    for sig in signals:
        if len(candidates) >= max_new:
            break
        ticker = sig['ticker']
        if ticker in data['positions']:
            continue
        execution_date = sig.get('execution_date')
        # 僅在「指定執行日 == 今日」才進場（t+1 open）。
        # 未來日 → 等到那天；過去日 → 視為過期不補單（避免用舊訊號舊限價進場）。
        if execution_date and execution_date != day:
            continue
        book_check = sig.get('book') or classify_book(ticker)
        if (book_check == 'satellite'
                and tiered_for_sizing.get('sat_entry_freeze', 0) >= 1.0):
            if verbose:
                print(f"   🛑 [v9 危機] 暫停 Satellite 新倉 {ticker}")
            continue
        bar = bars.get(ticker)
        if bar is None:
            if verbose:
                print(f"   ⚠️ 跳過 {ticker}: 無當日報價")
            continue
        limit_price = sig['entry']
        open_price = bar.get('open')
        low_price = bar.get('low')
        if low_price is not None and low_price > limit_price:
            if verbose:
                print(f"   ⏭️ 未成交 {ticker}: low {low_price:.1f} > limit {limit_price:.1f}")
            continue
        entry_price = (
            min(open_price, limit_price)
            if open_price is not None and open_price <= limit_price
            else limit_price
        )
        candidates.append((sig, entry_price))

    opened = 0
    for idx, (sig, entry_price) in enumerate(candidates):
        available_cash = max(data['capital'] - reserve_cash, 0)
        remaining_candidates = len(candidates) - idx
        if available_cash <= 0:
            if verbose:
                print(f"   💵 保留本金 10%，可投入現金不足，停止開倉")
            break

        gross_budget = available_cash / remaining_candidates
        ticker = sig['ticker']
        book = sig.get('book') or classify_book(ticker)
        if book == 'core':
            risk_mult = tiered_for_sizing.get('core_rotation_boost', 1.0)
        else:
            risk_mult = tiered_for_sizing.get('sat_rotation_boost', 1.0)
        adjusted_budget = gross_budget * risk_mult
        if verbose and adjusted_budget < gross_budget * 0.1:
            print(
                f"   📉 [v9 Tiered] {ticker} ({book}) 因高波動預測大幅降倉 "
                f"(scale={risk_mult:.2f})"
            )

        trade_amount = adjusted_budget / (1 + BUY_COST_RATE + SLIPPAGE)
        shares = int(trade_amount / entry_price)
        if shares <= 0:
            if verbose:
                print(f"   💵 資金不足 {ticker}: 無法在保留本金 10% + tiered scale 後買進")
            continue

        actual_trade_amount = shares * entry_price
        buy_cost = actual_trade_amount * (BUY_COST_RATE + SLIPPAGE)
        if data['capital'] - actual_trade_amount - buy_cost < reserve_cash:
            if verbose:
                print(f"   💵 資金不足 {ticker}: 保留本金 10% 後不開倉")
            continue

        data['capital'] -= (actual_trade_amount + buy_cost)
        data['positions'][ticker] = {
            'entry': entry_price,
            'tp': sig['tp'],
            'sl': sig['sl'],
            'entry_date': day,
            'shares': shares,
            'day_count': 0,
            'max_hold_days': sig.get('max_hold_days', MAX_HOLD_DAYS),
            'book': book,
            'tiered_scale_applied': round(risk_mult, 4),
        }
        opened += 1
        if verbose:
            emoji = '🟢' if book == 'core' else '🔵'
            scale_note = f" (tiered x{risk_mult:.2f})" if risk_mult < 0.95 else ""
            print(
                f"   {emoji} 開倉[{book}] {ticker} @ {entry_price:.1f} × {shares:,.0f} "
                f"(投入 {actual_trade_amount:,.0f}{scale_note}, TP {sig['tp']:.1f} / SL {sig['sl']:.1f})"
            )

    if verbose and opened:
        print(f"   ✅ 今日開倉 {opened} 檔 (v9 tiered scaling 已套用)")
    return opened


def _finalize_day_equity(data, day, bars, to_close):
    prices = {ticker: bar['close'] for ticker, bar in bars.items() if bar.get('close') is not None}
    for ticker, pos in data['positions'].items():
        if ticker in prices:
            # 記住最後成交收盤，作為日後資料缺口時的 MTM 回退值
            pos['last_close'] = prices[ticker]
        else:
            # 當日無報價：用最後已知收盤估值，而非進場價（避免 MTM 失真）
            prices[ticker] = pos.get('last_close', pos['entry'])

    core_eq, sat_eq, total_equity = compute_split_equity(data, prices, data['initial_capital'])
    data['equity_curve'].append({
        'date': day,
        'equity': round(total_equity, 0),
        'capital': round(data['capital'], 0),
        'n_positions': len(data['positions']),
        'n_closed_today': len(to_close),
    })
    data['core_equity_curve'].append({'date': day, 'equity': round(core_eq, 0)})
    data['sat_equity_curve'].append({'date': day, 'equity': round(sat_eq, 0)})

    try:
        if 'risk_adjusted_equity_curve' not in data:
            data['risk_adjusted_equity_curve'] = []
        last_scale = data.get('last_tiered', {}).get('overall', 1.0)
        adj = total_equity * (0.3 + 0.7 * last_scale)
        data['risk_adjusted_equity_curve'].append({'date': day, 'equity': round(adj, 0)})
    except Exception:
        pass

    try:
        pvt = _make_v3_pvt(data)
        ec, es, em = _vol_forecast_inputs(data, day)
        fvol = pvt.forecast_portfolio_ann_vol(ec, es, em)
        data['last_tiered'] = pvt.tiered_scale_factors(fvol)
    except Exception:
        data['last_tiered'] = {}

    return core_eq, sat_eq, total_equity


def _process_trading_day(data, day, bars, signals, verbose=True):
    tiered_for_sizing = _compute_tiered_for_sizing(data, day)
    if verbose:
        print(
            f"   📐 [v9] regime={tiered_for_sizing.get('vol_regime', 'normal')} "
            f"core_rot={tiered_for_sizing.get('core_rotation_boost', 1.0):.2f} "
            f"sat_rot={tiered_for_sizing.get('sat_rotation_boost', 1.0):.2f} "
            f"cooling={int(data.get('cooling_days_left', 0))}d"
        )

    to_close = _close_positions(data, day, bars, verbose=verbose)
    _apply_rotation_trims(data, bars, tiered_for_sizing, verbose=verbose)
    _open_positions(data, day, bars, signals, tiered_for_sizing, verbose=verbose)
    core_eq, sat_eq, total_equity = _finalize_day_equity(data, day, bars, to_close)

    if verbose:
        scales = data.get('last_tiered', {})
        fvol = scales.get('forecast_ann_vol', data.get('last_fvol', 0) or 0)
        print(
            f"   📐 Rotation | core={scales.get('core_rotation_boost', 1.0):.2f} "
            f"sat={scales.get('sat_rotation_boost', 1.0):.2f} "
            f"(fvol={fvol*100:.1f}%, target={TARGET_ANN_VOL_DEFAULT*100:.0f}%)"
        )
        total_return = (total_equity / data['initial_capital'] - 1) * 100
        print(
            f"\n   💰 總權益: {total_equity:,.0f} ({total_return:+.1f}%)  "
            f"[Core {core_eq:,.0f} / Sat {sat_eq:,.0f}]"
        )
        print(f"   📈 已完成交易: {len(data['closed_trades'])} 筆")

    return total_equity


def _latest_artifact(pattern: str) -> Optional[str]:
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def _load_signal_scores(day: str) -> dict:
    day_key = day.replace('-', '')
    path = f'artifacts/signals_{day_key}.csv'
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, index_col=0)
        return {str(idx): float(val) for idx, val in df['Score'].items()}
    except Exception:
        return {}


def _build_entry_schedule(trades_path: str, start_date: str) -> dict:
    df = pd.read_csv(trades_path)
    df['Entry_Date'] = pd.to_datetime(df['Entry_Date']).dt.strftime('%Y-%m-%d')
    start = start_date
    schedule = {}
    for _, row in df[df['Entry_Date'] >= start].iterrows():
        day = row['Entry_Date']
        schedule.setdefault(day, []).append({
            'ticker': str(row['Ticker']),
            'entry': float(row['Entry_Price']),
            'tp': float(row['TP_Price']),
            'sl': float(row['SL_Price']),
            'book': row.get('Book', classify_book(str(row['Ticker']))),
            'max_hold_days': MAX_HOLD_DAYS,
        })
    for day, entries in schedule.items():
        scores = _load_signal_scores(day)
        entries.sort(key=lambda e: scores.get(e['ticker'], 0), reverse=True)
    return schedule


def _download_historical_bars(tickers: list, start_date: str, end_date: str) -> dict:
    import yfinance as yf

    if not tickers:
        return {}

    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() - timedelta(days=5)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() + timedelta(days=5)

    def read_field(df, field, symbol):
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            if (field, symbol) not in df.columns:
                return None
            series = df[(field, symbol)].dropna()
        elif field in df.columns:
            series = df[field].dropna()
        else:
            return None
        return series if len(series) else None

    def parse_bars(df, symbol_map):
        parsed = {}
        for ticker, symbol in symbol_map.items():
            o = read_field(df, 'Open', symbol)
            h = read_field(df, 'High', symbol)
            l = read_field(df, 'Low', symbol)
            c = read_field(df, 'Close', symbol)
            if c is None:
                continue
            by_day = {}
            for idx in c.index:
                day = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)[:10]
                by_day[day] = {
                    'open': float(o.loc[idx]) if o is not None and idx in o.index else float(c.loc[idx]),
                    'high': float(h.loc[idx]) if h is not None and idx in h.index else float(c.loc[idx]),
                    'low': float(l.loc[idx]) if l is not None and idx in l.index else float(c.loc[idx]),
                    'close': float(c.loc[idx]),
                }
            parsed[ticker] = by_day
        return parsed

    bars = {}
    tw_map = {t: f"{t}.TW" for t in tickers}
    tw_df = yf.download(
        list(tw_map.values()), start=start_dt.isoformat(), end=end_dt.isoformat(),
        progress=False, auto_adjust=False, threads=True,
    )
    bars.update(parse_bars(tw_df, tw_map))

    missing = [t for t in tickers if t not in bars]
    if missing:
        two_map = {t: f"{t}.TWO" for t in missing}
        two_df = yf.download(
            list(two_map.values()), start=start_dt.isoformat(), end=end_dt.isoformat(),
            progress=False, auto_adjust=False, threads=True,
        )
        bars.update(parse_bars(two_df, two_map))
    return bars


def _seed_replay_equity_warmup(data: dict, start_date: str,
                               equity_path: Optional[str] = None) -> None:
    """Seed 60-day equity warmup from backtest so vol forecast matches event_backtest."""
    equity_path = equity_path or _latest_artifact('artifacts/equity_*.csv')
    if not equity_path or not os.path.exists(equity_path):
        return
    try:
        df = pd.read_csv(equity_path, parse_dates=['Date'])
        df = df[df['Date'] < pd.Timestamp(start_date)].sort_values('Date')
        if len(df) < 5:
            return
        warmup = df.tail(60)
        anchor = float(warmup.iloc[-1]['Equity'])
        if anchor <= 0:
            return
        scale = data['initial_capital'] / anchor
        for _, row in warmup.iterrows():
            scaled = float(row['Equity']) * scale
            day = row['Date'].strftime('%Y-%m-%d')
            data['equity_curve'].append({
                'date': day,
                'equity': round(scaled, 0),
                'capital': round(scaled, 0),
                'n_positions': 0,
                'n_closed_today': 0,
                'warmup': True,   # 僅供波動預測暖機，不計入顯示與績效統計
            })
            data['core_equity_curve'].append({'date': day, 'equity': round(scaled * 0.25, 0), 'warmup': True})
            data['sat_equity_curve'].append({'date': day, 'equity': round(scaled * 0.75, 0), 'warmup': True})
        print(f"   📈 權益暖機: {len(warmup)} 日 (來自 {os.path.basename(equity_path)})")
    except Exception as exc:
        print(f"   ⚠️ 權益暖機失敗: {exc}")


def _trading_days_from_bars(hist_bars: dict, start_date: str, end_date: str) -> list:
    days = set()
    for ticker_bars in hist_bars.values():
        for day in ticker_bars:
            if start_date <= day <= end_date:
                days.add(day)
    return sorted(days)


def _day_bars(hist_bars: dict, day: str, tickers: set) -> dict:
    out = {}
    for ticker in tickers:
        bar = hist_bars.get(ticker, {}).get(day)
        if bar:
            out[ticker] = bar
    return out


def _process_replay_day(data: dict, day: str, bars: dict, entry_signals: list) -> None:
    _process_trading_day(data, day, bars, entry_signals or [], verbose=False)


def replay_from_backtest(start_date: str, end_date: Optional[str] = None,
                         trades_path: Optional[str] = None,
                         equity_path: Optional[str] = None) -> dict:
    trades_path = trades_path or _latest_artifact('artifacts/trades_*.csv')
    if not trades_path:
        raise FileNotFoundError('找不到 artifacts/trades_*.csv，請先執行 ai_report.py')

    if end_date is None:
        meta_path = _latest_artifact('artifacts/metadata_*.json')
        if meta_path:
            with open(meta_path, encoding='utf-8') as f:
                end_date = json.load(f).get('report_date', date.today().isoformat())
        else:
            end_date = date.today().isoformat()

    print(f"🔄 Paper Replay v9: {start_date} → {end_date}")
    print(f"   📁 使用回測交易: {trades_path}")

    schedule = _build_entry_schedule(trades_path, start_date)
    trades_df = pd.read_csv(trades_path)
    tickers = set(trades_df['Ticker'].astype(str))
    for entries in schedule.values():
        tickers.update(e['ticker'] for e in entries)

    hist_bars = _download_historical_bars(sorted(tickers), start_date, end_date)
    trading_days = _trading_days_from_bars(hist_bars, start_date, end_date)
    if not trading_days:
        raise RuntimeError(f'無法取得 {start_date}~{end_date} 的歷史行情')

    data = {
        'start_date': start_date,
        'initial_capital': 200_000,
        'capital': 200_000,
        'positions': {},
        'closed_trades': [],
        'equity_curve': [],
        'core_equity_curve': [],
        'sat_equity_curve': [],
        'last_tiered': {},
        'last_fvol': None,
        'cooling_days_left': 0,
        'daily_signals': [],
    }
    _seed_replay_equity_warmup(data, start_date, equity_path)
    eq_path = equity_path or _latest_artifact('artifacts/equity_*.csv')
    if eq_path and os.path.exists(eq_path):
        try:
            data['_bt_equity_df'] = pd.read_csv(eq_path, parse_dates=['Date'])
        except Exception:
            pass

    for i, day in enumerate(trading_days):
        active = set(data['positions'].keys())
        entry_tickers = {e['ticker'] for e in schedule.get(day, [])}
        day_tickers = active | entry_tickers
        bars = _day_bars(hist_bars, day, day_tickers)
        _process_replay_day(data, day, bars, schedule.get(day, []))
        if (i + 1) % 10 == 0 or day == trading_days[-1]:
            eq = data['equity_curve'][-1]['equity']
            ret = (eq / data['initial_capital'] - 1) * 100
            print(f"   {day}: 權益 {eq:,.0f} ({ret:+.1f}%) | 持倉 {len(data['positions'])} | 已平 {len(data['closed_trades'])}")

    data.pop('_bt_equity_df', None)
    return data


def generate_html(data):
    """產出 paper trading 績效網頁。"""
    today = date.today().isoformat()
    initial = data['initial_capital']
    # 顯示與績效統計只用「正式 paper」資料，排除 vol 暖機種子（warmup 點）。
    equity_curve = [p for p in data['equity_curve'] if not p.get('warmup')]

    if not equity_curve:
        return

    latest_equity = equity_curve[-1]['equity']
    total_return = (latest_equity / initial - 1) * 100

    # 計算統計
    trades = data['closed_trades']
    n_trades = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / n_trades * 100 if n_trades > 0 else 0
    avg_pnl = sum(t['pnl_pct'] for t in trades) / n_trades if n_trades else 0
    total_profit = sum(t['pnl'] for t in wins) if wins else 0
    total_loss = abs(sum(t['pnl'] for t in losses)) if losses else 1
    pf = total_profit / total_loss if total_loss > 0 else 0

    # MDD
    peak = initial
    mdd = 0
    for pt in equity_curve:
        if pt['equity'] > peak:
            peak = pt['equity']
        dd = (pt['equity'] - peak) / peak * 100
        if dd < mdd:
            mdd = dd

    # 年化 (簡化)
    n_days = len(equity_curve)
    ann_return = total_return * (252 / max(n_days, 1))

    # ===== v9 Hybrid Tiered: 準備雙 book 曲線與 tiered 資料 =====
    core_curve = [p for p in (data.get('core_equity_curve', []) or []) if not p.get('warmup')]
    sat_curve = [p for p in (data.get('sat_equity_curve', []) or []) if not p.get('warmup')]
    last_tiered = data.get('last_tiered', {}) or {}

    core_latest = core_curve[-1]['equity'] if core_curve else latest_equity * 0.25
    sat_latest = sat_curve[-1]['equity'] if sat_curve else latest_equity * 0.75

    # 圖表用 JSON（優先用各自日期，長度不足時 fallback 對齊）
    dates_json = json.dumps([p['date'] for p in equity_curve])
    equity_json = json.dumps([p['equity'] for p in equity_curve])
    benchmark_json = json.dumps([initial] * len(equity_curve))

    core_dates = [p.get('date') for p in core_curve] or [p['date'] for p in equity_curve]
    core_json = json.dumps([p.get('equity', 0) for p in core_curve]) if core_curve else json.dumps([int(latest_equity*0.25)] * len(equity_curve))
    sat_json = json.dumps([p.get('equity', 0) for p in sat_curve]) if sat_curve else json.dumps([int(latest_equity*0.75)] * len(equity_curve))
    # v9 Option1 contrast
    risk_adj_curve = data.get('risk_adjusted_equity_curve', [])
    risk_adj_json = json.dumps([p.get('equity', latest_equity) for p in risk_adj_curve]) if risk_adj_curve else equity_json

    # tiered 摘要
    tiered_overall = last_tiered.get('overall', 1.0)
    tiered_fvol = last_tiered.get('forecast_ann_vol', 0.0)
    tiered_core_scale = last_tiered.get('core_rotation_boost', 1.0)
    tiered_sat_scale = last_tiered.get('sat_rotation_boost', 1.0)
    tiered_sat_mult = last_tiered.get('sat_mult', 1.0)
    tiered_target = last_tiered.get('target_ann_vol', TARGET_ANN_VOL_DEFAULT) * 100

    tiered_status = "🟢 正常" if tiered_sat_scale > 0.95 else ("🟡 降桿中" if tiered_sat_scale > 0.6 else "🔴 積極去風險")
    vol_regime = last_tiered.get('vol_regime', 'normal')
    core_rot = last_tiered.get('core_rotation_boost', 1.0)
    sat_rot = last_tiered.get('sat_rotation_boost', 1.0)
    tiered_reco = (
        f"資金輪動 [{vol_regime}]：高波動→Sat縮倉資金轉Core(boost {core_rot:.2f})；"
        f"回落→Core獲利了結回流Sat(boost {sat_rot:.2f})。"
    )

    # 交易清單 (最近 30 筆) — v9 顯示 book
    recent_trades = trades[-30:][::-1]
    trades_html = ""
    for t in recent_trades:
        color = '#4ade80' if t['pnl'] > 0 else '#f87171'
        emoji = '🟢' if t['pnl'] > 0 else '🔴'
        book = t.get('book', 'satellite')
        book_badge = '🟢' if book == 'core' else '🔵'
        trades_html += f"""
        <tr>
            <td>{t['exit_date']}</td>
            <td><b>{t['ticker']}</b> {book_badge}</td>
            <td>{t['entry']:.1f}</td>
            <td>{t['exit']:.1f}</td>
            <td style="color:{color};font-weight:700">{t['pnl_pct']:+.1f}%</td>
            <td>{t['reason']}</td>
            <td>{t['days_held']}天</td>
        </tr>"""

    # 持倉 (v9 含 Book)
    positions_html = ""
    for ticker, pos in data['positions'].items():
        book = pos.get('book', 'satellite')
        book_badge = '🟢 Core' if book == 'core' else '🔵 Sat'
        book_color = '#4ade80' if book == 'core' else '#60a5fa'
        positions_html += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td><span style="color:{book_color};font-weight:700">{book_badge}</span></td>
            <td>{pos['entry']:.1f}</td>
            <td>{pos['tp']:.1f}</td>
            <td>{pos['sl']:.1f}</td>
            <td>{pos['entry_date']}</td>
            <td>{pos.get('day_count', 0)}天</td>
        </tr>"""

    if not positions_html:
        positions_html = '<tr><td colspan="7" style="text-align:center;color:#888">目前無持倉</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Paper Trading · SURGE PRO — {today}</title>
    <meta name="description" content="TW Stocker Paper Trading 實時績效追蹤 — 追蹤 SURGE PRO（最強策略：去風險 + 分段強勢加碼）的每日訊號">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        h1 {{
            font-size: 1.8rem;
            background: linear-gradient(90deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 6px;
        }}
        .subtitle {{ color: #94a3b8; margin-bottom: 24px; font-size: 0.9rem; }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }}
        .metric {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(100, 116, 139, 0.3);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
        }}
        .metric .label {{ color: #94a3b8; font-size: 0.75rem; text-transform: uppercase; }}
        .metric .value {{ font-size: 1.5rem; font-weight: 700; margin-top: 4px; }}
        .metric .value.green {{ color: #4ade80; }}
        .metric .value.red {{ color: #f87171; }}
        .metric .value.blue {{ color: #60a5fa; }}
        .chart-box {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(100, 116, 139, 0.3);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
        }}
        .chart-box h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #cbd5e1; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }}
        th {{
            text-align: left;
            padding: 8px 10px;
            border-bottom: 2px solid #334155;
            color: #94a3b8;
            font-weight: 600;
        }}
        td {{
            padding: 8px 10px;
            border-bottom: 1px solid #1e293b;
        }}
        tr:hover {{ background: rgba(100, 116, 139, 0.1); }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 700;
        }}
        .badge-live {{ background: #22c55e33; color: #4ade80; }}
        .disclaimer {{
            margin-top: 24px;
            padding: 14px;
            background: rgba(251, 191, 36, 0.08);
            border: 1px solid rgba(251, 191, 36, 0.2);
            border-radius: 8px;
            font-size: 0.75rem;
            color: #fbbf24;
        }}
    </style>
</head>
<body>
<div class="container">
    <h1>📈 Paper Trading <span style="font-size:0.6em;color:#fda4af">· SURGE PRO（最強策略 · 分段強勢加碼）</span></h1>
    <p class="subtitle">
        <span class="badge badge-live">● LIVE</span>
        起始日 {data['start_date']} | 更新 {today} | 初始資金 {initial:,.0f} | Core-Satellite 分層 + Portfolio Vol Targeting (目標 {tiered_target:.0f}%)
    </p>

    <div class="metrics">
        <div class="metric">
            <div class="label">總權益</div>
            <div class="value {'green' if total_return > 0 else 'red'}">{latest_equity:,.0f}</div>
        </div>
        <div class="metric">
            <div class="label">總報酬</div>
            <div class="value {'green' if total_return > 0 else 'red'}">{total_return:+.1f}%</div>
        </div>
        <div class="metric">
            <div class="label">Core 權益</div>
            <div class="value blue">{core_latest:,.0f}</div>
        </div>
        <div class="metric">
            <div class="label">Satellite 權益</div>
            <div class="value blue">{sat_latest:,.0f}</div>
        </div>
        <div class="metric">
            <div class="label">最大回撤</div>
            <div class="value red">{mdd:.1f}%</div>
        </div>
        <div class="metric">
            <div class="label">勝率 / 交易數</div>
            <div class="value blue">{win_rate:.0f}% / {n_trades}</div>
        </div>
        <div class="metric">
            <div class="label">Profit Factor</div>
            <div class="value {'green' if pf > 1 else 'red'}">{pf:.2f}</div>
        </div>
        <div class="metric">
            <div class="label">持倉數</div>
            <div class="value blue">{len(data['positions'])}</div>
        </div>
    </div>

    <!-- v9 Tiered Risk Summary -->
    <div class="chart-box" style="border-left:4px solid #a78bfa; background:rgba(167,139,250,0.06);">
        <h2>🛡️ Hybrid Tiered Risk Budgeting (v9) — Portfolio Vol Target + Core/Satellite</h2>
        <div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 0;">
            <div class="metric" style="min-width:130px;">
                <div class="label">預測組合年化波動</div>
                <div class="value" style="color:#fbbf24;">{tiered_fvol*100:.1f}%</div>
            </div>
            <div class="metric" style="min-width:130px;">
                <div class="label">目標 / Overall Scale</div>
                <div class="value" style="color:#60a5fa;">{tiered_target:.0f}% / <b>{tiered_overall:.3f}</b></div>
            </div>
            <div class="metric" style="min-width:130px;">
                <div class="label">Core Rotation</div>
                <div class="value green">{tiered_core_scale:.3f} <span style="font-size:0.7em;color:#94a3b8;">(買滿×輪動)</span></div>
            </div>
            <div class="metric" style="min-width:130px;">
                <div class="label">Sat Rotation</div>
                <div class="value" style="color:#f87171;">{tiered_sat_scale:.3f} <span style="font-size:0.7em;color:#94a3b8;">(買滿×輪動)</span></div>
            </div>
            <div class="metric" style="min-width:130px;">
                <div class="label">狀態</div>
                <div class="value" style="font-size:1.1rem;">{tiered_status}</div>
            </div>
        </div>
        <div style="font-size:0.85rem; color:#cbd5e1; background:rgba(15,23,42,0.6); padding:8px 12px; border-radius:6px;">
            💡 {tiered_reco}<br>
            波動回落賣 Core alpha → 資金輪動 Satellite 動能（12日加碼視窗）。目標波動 15%。
        </div>
    </div>

    <div class="chart-box">
        <h2>權益曲線（v9: Total / Core / Satellite / Risk-Adjusted Tiered）</h2>
        <canvas id="equityChart" height="90"></canvas>
        <div style="font-size:0.7rem;color:#64748b;margin-top:4px;">藍=Total | 綠=Core(保護) | 紫=Sat | 橙=Risk-Adjusted (tiered de-lever 模擬)</div>
    </div>

    <div class="chart-box">
        <h2>🔓 目前持倉（v9 雙 Book）</h2>
        <table>
            <tr><th>股票</th><th>Book</th><th>進場價</th><th>停利</th><th>停損</th><th>進場日</th><th>持有</th></tr>
            {positions_html}
        </table>
        <div style="margin-top:8px;font-size:0.75rem;color:#94a3b8;">🟢 = Core（高信心、較保護）　🔵 = Satellite（戰術動能，嚴格受 vol target 約束）</div>
    </div>

    <div class="chart-box">
        <h2>📋 近期交易（最近 30 筆） — 含 Book 標記</h2>
        <table>
            <tr><th>日期</th><th>股票 / Book</th><th>進場</th><th>出場</th><th>損益</th><th>原因</th><th>持有</th></tr>
            {trades_html}
        </table>
    </div>

    <div class="disclaimer">
        ⚠️ <b>免責聲明：</b>此為 Paper Trading 模擬績效，非真實交易。歷史模擬不代表未來報酬。
        v9 Hybrid Tiered：Vol Target 15%，Core/Sat 買滿後高波動才輪動，非永久壓縮 alpha。
        所有 scale 與 Core 選取決策寫入 experiment_registry。策略 alpha 維持 v8.5 + SR v2 驗證結果。投資有風險，決策請自行負責。
    </div>

    <div style="margin-top:12px;font-size:0.75rem;color:#64748b;">
        Core 結構性龍頭示例：2330 (TSMC)、2454 等（詳見 strategy/core_holdings.py）。Core 基礎曝險較高、尾部風險容忍較寬。
    </div>
</div>

<script>
const ctx = document.getElementById('equityChart').getContext('2d');
new Chart(ctx, {{
    type: 'line',
    data: {{
        labels: {dates_json},
        datasets: [
            {{
                label: 'Total (合併)',
                data: {equity_json},
                borderColor: '#60a5fa',
                backgroundColor: 'rgba(96, 165, 250, 0.12)',
                fill: true,
                tension: 0.25,
                pointRadius: 1.5,
                borderWidth: 2.5,
            }},
            {{
                label: 'Core (高信心，保護)',
                data: {core_json},
                borderColor: '#4ade80',
                backgroundColor: 'rgba(74, 222, 128, 0.08)',
                fill: false,
                tension: 0.25,
                pointRadius: 1,
                borderWidth: 2,
                borderDash: [2,2],
            }},
            {{
                label: 'Satellite (戰術，嚴格)',
                data: {sat_json},
                borderColor: '#a78bfa',
                backgroundColor: 'rgba(167, 139, 250, 0.06)',
                fill: false,
                tension: 0.25,
                pointRadius: 1,
                borderWidth: 2,
            }},
            {{
                label: 'Risk-Adjusted (v9 Tiered)',
                data: {risk_adj_json},
                borderColor: '#fb923c',
                backgroundColor: 'rgba(251, 146, 60, 0.08)',
                fill: false,
                tension: 0.3,
                pointRadius: 1,
                borderWidth: 2,
                borderDash: [4,2],
            }},
            {{
                label: '初始資金',
                data: {benchmark_json},
                borderColor: '#475569',
                borderDash: [5, 5],
                fill: false,
                pointRadius: 0,
                borderWidth: 1,
            }}
        ]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ 
                labels: {{ color: '#94a3b8', boxWidth: 12, font: {{size: 11}} }},
                position: 'top'
            }},
        }},
        scales: {{
            x: {{ ticks: {{ color: '#64748b', maxTicksLimit: 12 }}, grid: {{ color: '#1e293b' }} }},
            y: {{ ticks: {{ color: '#64748b', callback: v => (v/1000).toFixed(0)+'K' }}, grid: {{ color: '#1e293b' }} }},
        }}
    }}
}});

// 額外提示：tiered 狀態
console.log('%c[v9 Tiered] overall=' + {tiered_overall} + ' fvol=' + ({tiered_fvol}*100).toFixed(1) + '%', 'color:#64748b');
</script>
</body>
</html>"""

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"   🌐 績效網頁已更新: {HTML_FILE}")


def main():
    parser = argparse.ArgumentParser(description='Paper Trading 自動追蹤器 v9 (Hybrid Tiered Risk Budgeting)')
    parser.add_argument('--reset', action='store_true', help='清除所有記錄重新開始')
    parser.add_argument('--replay-from', type=str, metavar='YYYY-MM-DD',
                        help='依最新回測交易從指定日期重播 paper（會清除舊記錄）')
    parser.add_argument('--replay-to', type=str, metavar='YYYY-MM-DD',
                        help='重播結束日（預設為最新回測 report_date）')
    args = parser.parse_args()

    if args.reset:
        for f in [DATA_FILE, HTML_FILE]:
            if os.path.exists(f):
                os.remove(f)
        print("🔄 已清除所有 paper trading 記錄")
        return

    if args.replay_from:
        for f in [DATA_FILE, HTML_FILE]:
            if os.path.exists(f):
                os.remove(f)
        data = replay_from_backtest(args.replay_from, args.replay_to)
        save_data(data)
        generate_html(data)
        print("✅ Paper Replay 完成")
        return

    data = load_data()
    update_tracker(data)
    save_data(data)
    generate_html(data)
    print("✅ Paper Tracker 完成")


if __name__ == '__main__':
    main()
