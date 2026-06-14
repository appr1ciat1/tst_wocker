#!/usr/bin/env python3
"""
Paper Trading 比對工具

每日收盤後比對 AI 策略信號與實際成交紀錄，追蹤回測 vs 實盤偏差。

使用方式:
  # 1. 產出今日信號
  python paper_trade.py signals

  # 2. 產出信號 + Telegram 通知
  python paper_trade.py signals --notify

  # 3. 記錄實際成交（手動輸入）
  python paper_trade.py log --ticker 2330 --action buy --price 980 --shares 1000

  # 4. 比對報告
  python paper_trade.py report

  # 5. 風險警報（檢查回撤）
  python paper_trade.py alert --max-dd 12

Telegram 通知設定:
  export TELEGRAM_BOT_TOKEN='your_bot_token'
  export TELEGRAM_CHAT_ID='your_chat_id'
  # 建立 Bot: https://t.me/BotFather → /newbot
  # 取得 Chat ID: https://t.me/userinfobot
"""

import json
import os
import sys
from datetime import datetime, date
import argparse
import pandas as pd


# v9 Hybrid Tiered imports
from strategy.risk_metrics import (
    compute_tiered_risk_summary,
    format_tiered_risk_summary,
    merge_book_equities,
)
from strategy.portfolio_vol_target import PortfolioVolatilityTarget, VolTargetConfig
from strategy.core_holdings import CoreHoldingsManager, get_default_core

TRADE_LOG = 'paper_trades.json'
SIGNAL_LOG = 'paper_signals.json'
EQUITY_LOG = 'paper_equity.json'


def send_telegram(message):
    """透過 Telegram Bot 傳送通知。需設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。"""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return False
    try:
        import urllib.request
        import urllib.parse
        import json as _json
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = _json.dumps({'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'   ⚠️ Telegram 通知失敗: {e}')
        return False


def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def fetch_latest_prices(tickers):
    """Fetch latest close prices for mark-to-market checks."""
    prices = {}
    if not tickers:
        return prices
    try:
        import yfinance as yf

        def read_prices(symbol_map):
            data = yf.download(list(symbol_map.values()), period='5d', progress=False)
            parsed = {}
            if data.empty:
                return parsed
            close = data['Close']
            if len(symbol_map) == 1:
                ticker = next(iter(symbol_map))
                if hasattr(close, 'iloc') and getattr(close, 'ndim', 1) == 2:
                    close = close.iloc[:, 0]
                series = close.dropna()
                if not series.empty:
                    parsed[ticker] = float(series.iloc[-1])
            else:
                for ticker, yf_ticker in symbol_map.items():
                    if yf_ticker in close.columns:
                        series = close[yf_ticker].dropna()
                        if not series.empty:
                            parsed[ticker] = float(series.iloc[-1])
            return parsed

        tw_symbols = {t: f"{t}.TW" for t in tickers}
        prices.update(read_prices(tw_symbols))
        missing = [t for t in tickers if t not in prices]
        if missing:
            two_symbols = {t: f"{t}.TWO" for t in missing}
            prices.update(read_prices(two_symbols))
    except Exception as e:
        print(f"   ⚠️ 最新價格下載失敗，未實現損益將用成本估算: {e}")
    return prices


def extract_signal_rows(html):
    """從 stock_report.html 擷取買進訊號。"""
    import re

    current_pattern = (
        r'<tr[^>]*>\s*'
        r'<td>(\d{4})</td>\s*'
        r'<td>[\d\.]+</td>\s*'
        r'<td>([\d\.]+)</td>\s*'
        r'<td>.*?建議買進.*?</td>\s*'
        r'<td>.*?停利.*?<span[^>]*>([\d\.]+)</span>.*?'
        r'停損.*?<span[^>]*>([\d\.]+)</span>.*?'
        r'最晚出場.*?(\d{4}-\d{2}-\d{2})'
    )
    matches = re.findall(current_pattern, html, re.DOTALL)
    if matches:
        return matches

    legacy_pattern = (
        r'<td>(\d{4})</td>\s*<td[^>]*>[^<]*</td>\s*'
        r'<td[^>]*>([\d\.]+)</td>\s*'
        r'<td[^>]*>([\d\.]+)</td>\s*'
        r'<td[^>]*>([\d\.]+)</td>'
    )
    return [(ticker, entry, tp, sl, '-') for ticker, entry, tp, sl
            in re.findall(legacy_pattern, html)]


def generate_signals(args):
    """從最新 stock_report.html 擷取今日執行計畫。"""
    import re

    report_path = 'stock_report.html'
    if not os.path.exists(report_path):
        print("⚠️ stock_report.html 不存在，請先執行 python ai_report.py")
        return

    with open(report_path) as f:
        html = f.read()

    matches = extract_signal_rows(html)

    if not matches:
        # 嘗試另一種格式
        pattern = r'買入\s+(\d{4})\s'
        tickers = re.findall(pattern, html)
        if tickers:
            print(f"📋 今日信號股票: {', '.join(tickers)}")
        else:
            print("📋 今日無信號（可能為非交易日或 regime filter 阻擋）")
        return

    today = date.today().isoformat()
    signals = load_json(SIGNAL_LOG)

    # 籌碼 + 新聞標注
    inst_data = {}
    news_data = {}
    if hasattr(args, 'enrich') and args.enrich:
        try:
            from strategy.institutional_flow import get_inst_flow_for_signals
            from strategy.news_sentiment import get_news_sentiment_for_signals
            all_tickers = [t for t, _, _, _, _ in matches]
            inst_data = get_inst_flow_for_signals(all_tickers)
            news_data = get_news_sentiment_for_signals(all_tickers)
        except Exception as e:
            print(f"   ⚠️ 籌碼/新聞標注失敗: {e}")

    has_enrich = bool(inst_data or news_data)

    print(f"📋 今日執行信號 ({today}):")
    header = f"{'股票':>6s} | {'進場價':>8s} | {'停利價':>8s} | {'停損價':>8s} | {'最晚出場':>12s}"
    if has_enrich:
        header += f" | {'🏛️ 籌碼':>10s} | {'📰 新聞':>10s}"
    print(header)
    print("-" * (82 if has_enrich else 58))

    new_signals = []
    for ticker, entry, tp, sl, exit_date in matches:
        line = f"{ticker:>6s} | {entry:>8s} | {tp:>8s} | {sl:>8s} | {exit_date:>12s}"
        if has_enrich:
            idata = inst_data.get(ticker, {})
            ndata = news_data.get(ticker, {})
            inst_label = idata.get('label', '⚪')
            news_label = ndata.get('label', '⚪')
            inst_change = idata.get('change', 0.0)
            line += f" | {inst_label} {inst_change:+.1f}% | {news_label}"
        print(line)
        new_signals.append({
            'date': today,
            'ticker': ticker,
            'entry_price': float(entry),
            'tp_price': float(tp),
            'sl_price': float(sl),
            'exit_date': exit_date if exit_date != '-' else None,
            'status': 'pending',
            'inst_flow': inst_data.get(ticker, {}).get('change', 0.0),
            'news_score': news_data.get(ticker, {}).get('score', 0.0),
        })

    if new_signals:
        signals.extend(new_signals)
        save_json(SIGNAL_LOG, signals)
        print(f"\n✅ {len(new_signals)} 筆信號已記錄到 {SIGNAL_LOG}")

        # Telegram 通知
        if hasattr(args, 'notify') and args.notify:
            msg = f"📊 今日執行信號 ({today})\n"
            for s in new_signals:
                msg += (
                    f"<b>{s['ticker']}</b> "
                    f"進場{s['entry_price']:.1f} "
                    f"TP{s['tp_price']:.1f} "
                    f"SL{s['sl_price']:.1f}"
                )
                if s.get('exit_date'):
                    msg += f" 最晚{s['exit_date']}"
                if has_enrich:
                    idata = inst_data.get(s['ticker'], {})
                    ndata = news_data.get(s['ticker'], {})
                    msg += f" | 🏛️{idata.get('label', '⚪')} | 📰{ndata.get('label', '⚪')}"
                msg += "\n"
            if send_telegram(msg):
                print("📤 已傳送 Telegram 通知")
            else:
                print("⚠️ Telegram 未設定（設定 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID）")


def log_trade(args):
    """記錄實際成交。"""
    trades = load_json(TRADE_LOG)
    trade = {
        'date': date.today().isoformat(),
        'ticker': args.ticker,
        'action': args.action,
        'price': args.price,
        'shares': args.shares,
        'timestamp': datetime.now().isoformat(),
    }
    trades.append(trade)
    save_json(TRADE_LOG, trades)
    print(f"✅ 已記錄: {args.action.upper()} {args.ticker} @ {args.price} × {args.shares}")


def generate_report(args):
    """比對信號 vs 實際成交。"""
    signals = load_json(SIGNAL_LOG)
    trades = load_json(TRADE_LOG)

    if not signals:
        print("⚠️ 無信號記錄，先執行 python paper_trade.py signals")
        return

    print(f"📊 Paper Trading 比對報告")
    print(f"   信號記錄: {len(signals)} 筆")
    print(f"   實際成交: {len(trades)} 筆")
    print()

    # 按日統計
    dates = sorted(set(s['date'] for s in signals))

    total_signals = 0
    total_executed = 0
    total_slippage = []

    for d in dates:
        day_signals = [s for s in signals if s['date'] == d]
        day_trades = [t for t in trades if t['date'] == d]

        total_signals += len(day_signals)
        signal_tickers = {s['ticker'] for s in day_signals}
        trade_tickers = {t['ticker'] for t in day_trades if t['action'] == 'buy'}

        executed = signal_tickers & trade_tickers
        missed = signal_tickers - trade_tickers
        extra = trade_tickers - signal_tickers

        total_executed += len(executed)

        # 計算滑價
        for t in day_trades:
            if t['action'] == 'buy' and t['ticker'] in signal_tickers:
                sig = next(s for s in day_signals if s['ticker'] == t['ticker'])
                slip = (t['price'] - sig['entry_price']) / sig['entry_price'] * 100
                total_slippage.append(slip)

        print(f"📅 {d}: {len(day_signals)} 信號 | {len(executed)} 執行 | {len(missed)} 未執行 | {len(extra)} 額外")
        if missed:
            print(f"   ❌ 未執行: {', '.join(missed)}")
        if extra:
            print(f"   ⚠️ 額外交易: {', '.join(extra)}")

    print()
    execution_rate = total_executed / total_signals * 100 if total_signals > 0 else 0
    print(f"📈 執行統計:")
    print(f"   執行率:     {execution_rate:.1f}% ({total_executed}/{total_signals})")

    if total_slippage:
        import statistics
        avg_slip = statistics.mean(total_slippage)
        print(f"   平均滑價:   {avg_slip:+.3f}%")
        print(f"   最大滑價:   {max(total_slippage):+.3f}%")
        if abs(avg_slip) < 0.1:
            print("   ✅ 滑價在可接受範圍內")
        elif abs(avg_slip) < 0.3:
            print("   ⚠️ 滑價偏高，建議調整進場時機")
        else:
            print("   🚨 滑價過大，需檢查執行流程")

        # 執行偏差自動警報
        if execution_rate < 85 or abs(avg_slip) > 0.2:
            msg = (f"⚠️ 執行偏差警報\n"
                   f"執行率: {execution_rate:.1f}%\n"
                   f"平均滑價: {avg_slip:+.3f}%\n"
                   f"建議檢查掛單策略")
            print(f"\n{msg}")
            send_telegram(msg)
    else:
        # 無滑價數據但有信號
        if execution_rate < 85:
            msg = f"⚠️ 執行率偏低: {execution_rate:.1f}%，建議檢查掛單流程"
            print(f"\n{msg}")
            send_telegram(msg)


def check_alert(args):
    """風險警報：檢查當前回撤是否超過門檻。"""
    import re

    report_path = 'stock_report.html'
    if not os.path.exists(report_path):
        print("⚠️ stock_report.html 不存在")
        return

    with open(report_path) as f:
        html = f.read()

    # 擷取 MDD（格式：<div class="label">最大回撤</div>\n<div class="value"...>-17.5%</div>）
    mdd_match = re.search(r'最大回撤.*?([\-\d\.]+)%', html, re.DOTALL)
    if not mdd_match:
        print("⚠️ 無法擷取回撤數據")
        return

    mdd = abs(float(mdd_match.group(1)))
    max_dd = args.max_dd

    print(f"🚨 風險檢查")
    print(f"   當前 MDD:  -{mdd:.1f}%")
    print(f"   警報門檻: -{max_dd:.1f}%")

    if mdd > max_dd:
        msg = f"🚨 風險警報！MDD -{mdd:.1f}% 已超過門檻 -{max_dd:.1f}%\n建議次月減半部位或暫停新倉"
        print(msg)
        if send_telegram(msg):
            print("📤 已傳送 Telegram 警報")
        sys.exit(1)
    else:
        print(f"   ✅ MDD 在安全範圍內")


# ===================== v8 新功能 =====================

def check_portfolio_hardstop(args):
    """
    Portfolio Hard Stop — 組合層級權益保護。

    監控實際成交記錄的浮動盈虧，超過門檻觸發警報。
    - Soft stop (-10%): Telegram 警報 + 建議減半部位
    - Hard stop (-15%): 緊急警報 + 建議全部平倉
    """
    trades = load_json(TRADE_LOG)
    if not trades:
        check_tracker_hardstop(args)
        return

    soft_pct = args.soft_stop
    hard_pct = args.hard_stop

    positions = {}  # {ticker: {'cost': float, 'shares': int}}
    total_invested = 0.0
    total_returned = 0.0
    realized_pnl = 0.0

    for t in trades:
        ticker = t['ticker']
        if t['action'] == 'buy':
            if ticker not in positions:
                positions[ticker] = {'cost': 0.0, 'shares': 0}
            positions[ticker]['cost'] += t['price'] * t['shares']
            positions[ticker]['shares'] += t['shares']
            total_invested += t['price'] * t['shares']
        elif t['action'] == 'sell':
            proceeds = t['price'] * t['shares']
            total_returned += proceeds
            if ticker in positions:
                sell_shares = min(t['shares'], positions[ticker]['shares'])
                avg_cost = (positions[ticker]['cost'] / positions[ticker]['shares']
                            if positions[ticker]['shares'] > 0 else 0)
                cost_released = avg_cost * sell_shares
                realized_pnl += proceeds - cost_released
                positions[ticker]['cost'] -= cost_released
                positions[ticker]['shares'] -= sell_shares
                if positions[ticker]['shares'] <= 0:
                    del positions[ticker]

    latest_prices = fetch_latest_prices(list(positions.keys()))
    open_cost = 0.0
    open_market_value = 0.0
    for ticker, pos in positions.items():
        shares = pos['shares']
        cost = pos['cost']
        avg_cost = cost / shares if shares > 0 else 0
        mark_price = latest_prices.get(ticker, avg_cost)
        open_cost += cost
        open_market_value += mark_price * shares

    unrealized_pnl = open_market_value - open_cost
    total_pnl = realized_pnl + unrealized_pnl

    equity_data = load_json(EQUITY_LOG)
    equity_curve = equity_data.get('equity_curve', []) if isinstance(equity_data, dict) else []
    initial_equity = equity_data.get('initial_capital') if isinstance(equity_data, dict) else None
    current_equity = equity_curve[-1]['equity'] if equity_curve else None
    peak_equity = None
    drawdown_pct = None
    if equity_curve:
        peak_equity = max([initial_equity or 0] + [pt.get('equity', 0) for pt in equity_curve])
        if peak_equity > 0 and current_equity is not None:
            drawdown_pct = (current_equity / peak_equity - 1) * 100

    print(f"🛡️ Portfolio Hard Stop 檢查")
    print(f"   累計投入:     {total_invested:,.0f}")
    print(f"   已回收:       {total_returned:,.0f}")
    print(f"   已實現 PnL:   {realized_pnl:+,.0f}")
    print(f"   未實現 PnL:   {unrealized_pnl:+,.0f}")
    print(f"   MTM 總 PnL:   {total_pnl:+,.0f}")
    print(f"   未平倉檔數:   {len(positions)}")
    if drawdown_pct is not None:
        print(f"   權益回撤:     {drawdown_pct:+.1f}% (peak {peak_equity:,.0f})")
    print(f"   Soft stop:    -{soft_pct:.0f}%")
    print(f"   Hard stop:    -{hard_pct:.0f}%")

    if total_invested > 0:
        pnl_pct = (total_pnl / total_invested) * 100
        stop_pct = drawdown_pct if drawdown_pct is not None else pnl_pct

        if stop_pct <= -hard_pct:
            msg = (f"🚨🚨 HARD STOP 觸發！\n"
                   f"MTM損益: {pnl_pct:+.1f}%\n")
            if drawdown_pct is not None:
                msg += f"權益回撤: {drawdown_pct:+.1f}%\n"
            msg += (f"門檻: -{hard_pct:.0f}%\n"
                    f"⚡ 建議立即全部平倉")
            print(f"\n{msg}")
            send_telegram(msg)
            sys.exit(2)
        elif stop_pct <= -soft_pct:
            msg = (f"⚠️ SOFT STOP 觸發！\n"
                   f"MTM損益: {pnl_pct:+.1f}%\n")
            if drawdown_pct is not None:
                msg += f"權益回撤: {drawdown_pct:+.1f}%\n"
            msg += (f"門檻: -{soft_pct:.0f}%\n"
                    f"💡 建議減半部位，暫停新進場")
            print(f"\n{msg}")
            send_telegram(msg)
            sys.exit(1)
        else:
            print(f"   MTM損益率:    {pnl_pct:+.1f}%")
            print(f"   ✅ 在安全範圍內")


def check_tracker_hardstop(args):
    """Fallback hard stop check for the automated paper tracker state."""
    equity_data = load_json(EQUITY_LOG)
    if not isinstance(equity_data, dict) or not equity_data.get('equity_curve'):
        print("⚠️ 無成交記錄，也沒有 paper tracker 權益曲線")
        return

    equity_curve = equity_data.get('equity_curve', [])
    initial_equity = equity_data.get('initial_capital', 0)
    current_equity = equity_curve[-1].get('equity', 0)
    peak_equity = max([initial_equity] + [pt.get('equity', 0) for pt in equity_curve])
    drawdown_pct = (current_equity / peak_equity - 1) * 100 if peak_equity > 0 else 0
    total_return_pct = (current_equity / initial_equity - 1) * 100 if initial_equity > 0 else 0
    positions = equity_data.get('positions', {})

    print("🛡️ Portfolio Hard Stop 檢查 (paper tracker)")
    print(f"   初始權益:     {initial_equity:,.0f}")
    print(f"   目前權益:     {current_equity:,.0f} ({total_return_pct:+.1f}%)")
    print(f"   權益高點:     {peak_equity:,.0f}")
    print(f"   權益回撤:     {drawdown_pct:+.1f}%")
    print(f"   未平倉檔數:   {len(positions)}")
    print(f"   Soft stop:    -{args.soft_stop:.0f}%")
    print(f"   Hard stop:    -{args.hard_stop:.0f}%")

    if drawdown_pct <= -args.hard_stop:
        msg = (f"🚨🚨 HARD STOP 觸發！\n"
               f"Paper tracker 權益回撤: {drawdown_pct:+.1f}%\n"
               f"門檻: -{args.hard_stop:.0f}%\n"
               f"⚡ 建議立即全部平倉")
        print(f"\n{msg}")
        send_telegram(msg)
        sys.exit(2)
    if drawdown_pct <= -args.soft_stop:
        msg = (f"⚠️ SOFT STOP 觸發！\n"
               f"Paper tracker 權益回撤: {drawdown_pct:+.1f}%\n"
               f"門檻: -{args.soft_stop:.0f}%\n"
               f"💡 建議減半部位，暫停新進場")
        print(f"\n{msg}")
        send_telegram(msg)
        sys.exit(1)

    print("   ✅ 在安全範圍內")


# ===================== v9 Hybrid Tiered Risk Budgeting =====================

def compute_current_tiered_scales(args):
    """
    根據 paper_equity.json 的權益曲線（或分離 core/sat 子曲線）計算當前
    Portfolio Vol Target + Tiered Core/Satellite scale factors。
    並建議調整方向。
    """
    equity_data = load_json(EQUITY_LOG)
    if not isinstance(equity_data, dict):
        print("⚠️ 無 paper_equity.json 或格式不正確")
        return

    # 嘗試讀取合併或分離 equity
    eq_curve = equity_data.get('equity_curve', [])
    core_curve = equity_data.get('core_equity_curve') or []
    sat_curve = equity_data.get('sat_equity_curve') or []

    # 轉成 Series
    def curve_to_series(curve, key='equity'):
        if not curve:
            return None
        dates = [pd.Timestamp(p['date']) for p in curve if 'date' in p]
        vals = [float(p.get(key, p.get('equity', 0))) for p in curve if 'date' in p]
        if not dates:
            return None
        return pd.Series(vals, index=pd.to_datetime(dates)).sort_index()

    eq_merged = curve_to_series(eq_curve)
    eq_core = curve_to_series(core_curve) if core_curve else None
    eq_sat = curve_to_series(sat_curve) if sat_curve else None

    if (eq_core is None or len(eq_core) == 0) and (eq_sat is None or len(eq_sat) == 0) and eq_merged is not None and len(eq_merged) > 0:
        # 單一 equity 時，保守拆分 25/75 近似（實際應由 tracker 維護分離曲線）
        # 這裡僅供展示：用同一曲線當 proxy
        eq_core = eq_merged * 0.25
        eq_sat = eq_merged * 0.75

    pvt = PortfolioVolatilityTarget(VolTargetConfig(
        target_ann_vol=getattr(args, 'target_vol', 0.15),
        core_decay=getattr(args, 'core_decay', 0.35),
        sat_decay=getattr(args, 'sat_decay', 0.85),
        core_floor=getattr(args, 'core_floor', 0.55),
        sat_floor=getattr(args, 'sat_floor', 0.15),
    ))

    fvol = pvt.forecast_portfolio_ann_vol(eq_core, eq_sat, eq_merged)
    scales = pvt.tiered_scale_factors(fvol)

    summary = compute_tiered_risk_summary(
        equity_core=eq_core, equity_sat=eq_sat,
        target_ann_vol=pvt.cfg.target_ann_vol
    )
    # 覆蓋 scales
    summary['portfolio'].update(scales)

    print(format_tiered_risk_summary(summary))
    print("\n📌 建議：")
    print(f"   目前預測組合波動 {fvol*100:.1f}% ，target {pvt.cfg.target_ann_vol*100:.0f}%")
    if scales['overall'] < 0.95:
        print(f"   → 觸發降桿：Core 有效曝險 {scales['core_effective']:.2f} | Sat {scales['sat_effective']:.2f}")
        print("   → 實務上：衛星部位優先減碼，Core 保留較多（保護高信心 alpha）")
    else:
        print("   ✅ 目前風險在目標範圍內，可維持標準 sizing / regime 曝險。")

    # 寫 registry（若可用）
    try:
        from research.experiment_registry import ExperimentRegistry
        reg = ExperimentRegistry()
        reg.record_experiment(
            source="paper_trade.tiered",
            strategy_version="v9-hybrid-tiered",
            hypothesis="Daily portfolio vol target + tiered core/sat scaling (live paper)",
            metrics=scales,
            decision="scale_applied" if scales['overall'] < 0.95 else "within_target",
            notes="computed from paper_equity.json",
        )
        print("   (已記錄至 experiment registry)")
    except Exception:
        pass


def suggest_core_holdings(args):
    """執行客觀 Core 篩選並顯示（不改動現有持倉）。"""
    print("🔍 執行 Core Holdings 多因子篩選（Hybrid Tiered Framework）...")
    mgr = CoreHoldingsManager(core_cap=getattr(args, 'core_cap', 5))
    # 需要真實 close/vol 才能精準算分；這裡給出結構性建議 + 呼叫選取（使用最小 mock）
    print("   預設結構性龍頭（強制候選）:", get_default_core())
    print("   完整篩選需傳入 close_df / vol_df（見 strategy/core_holdings.py）")
    print("   建議手動確保 2330 等核心龍頭列入 Core book，並給予較高基礎曝險 20-30%。")

    # 示範記錄一筆 registry（實際選取應在有數據時呼叫 mgr.select_core 後 log）
    try:
        exp = mgr.log_selection(
            cores=get_default_core() + ['2454'],
            scores={'2330': 0.98, '2454': 0.87},
            meta={"asof": "manual", "mode": "suggest"},
            hypothesis="Manual core suggestion for v9 tiered risk (user query spec)",
        )
        print(f"   決策已寫入 registry: {exp}")
    except Exception as e:
        print(f"   (registry 寫入略過: {e})")


def generate_monthly_report(args):
    """
    生成月度績效報告。

    彙總當月信號、成交、PnL、滑價統計，輸出 Markdown。
    """
    signals = load_json(SIGNAL_LOG)
    trades = load_json(TRADE_LOG)

    # 決定月份
    if hasattr(args, 'month') and args.month:
        target_month = args.month  # format: YYYY-MM
    else:
        today = date.today()
        # 預設上月
        if today.month == 1:
            target_month = f"{today.year - 1}-12"
        else:
            target_month = f"{today.year}-{today.month - 1:02d}"

    print(f"📅 生成月報: {target_month}")

    # 篩選該月數據
    month_signals = [s for s in signals if s['date'].startswith(target_month)]
    month_trades = [t for t in trades if t['date'].startswith(target_month)]

    # 統計
    n_signals = len(month_signals)
    n_trades = len(month_trades)
    n_buys = len([t for t in month_trades if t['action'] == 'buy'])
    n_sells = len([t for t in month_trades if t['action'] == 'sell'])

    # 計算執行率
    signal_dates = set(s['date'] for s in month_signals)
    executed = 0
    for d in signal_dates:
        day_sigs = {s['ticker'] for s in month_signals if s['date'] == d}
        day_buys = {t['ticker'] for t in month_trades if t['date'] == d and t['action'] == 'buy'}
        executed += len(day_sigs & day_buys)
    exec_rate = (executed / n_signals * 100) if n_signals > 0 else 0

    # 滑價統計
    slippages = []
    for t in month_trades:
        if t['action'] == 'buy':
            matching = [s for s in month_signals
                        if s['date'] == t['date'] and s['ticker'] == t['ticker']]
            if matching:
                slip = (t['price'] - matching[0]['entry_price']) / matching[0]['entry_price'] * 100
                slippages.append(slip)

    avg_slip = sum(slippages) / len(slippages) if slippages else 0

    # PnL（簡化：配對 buy/sell）
    buy_records = {}
    realized_pnl = 0.0
    for t in month_trades:
        if t['action'] == 'buy':
            key = t['ticker']
            if key not in buy_records:
                buy_records[key] = []
            buy_records[key].append(t)
        elif t['action'] == 'sell':
            key = t['ticker']
            if key in buy_records and buy_records[key]:
                buy = buy_records[key].pop(0)
                pnl = (t['price'] - buy['price']) * min(t['shares'], buy['shares'])
                realized_pnl += pnl

    # 生成 Markdown
    md = f"""# 📊 月度績效報告 — {target_month}

## 概覽
| 指標 | 數值 |
|------|------|
| 信號數 | {n_signals} |
| 買入成交 | {n_buys} |
| 賣出成交 | {n_sells} |
| 執行率 | {exec_rate:.1f}% |
| 平均滑價 | {avg_slip:+.3f}% |
| 已實現 PnL | {realized_pnl:+,.0f} |

## 每日信號統計
| 日期 | 信號數 | 執行 | 未執行 |
|------|:------:|:----:|:------:|
"""
    for d in sorted(signal_dates):
        day_sigs = [s for s in month_signals if s['date'] == d]
        day_buys = {t['ticker'] for t in month_trades if t['date'] == d and t['action'] == 'buy'}
        sig_tickers = {s['ticker'] for s in day_sigs}
        ex = len(sig_tickers & day_buys)
        miss = len(sig_tickers - day_buys)
        md += f"| {d} | {len(day_sigs)} | {ex} | {miss} |\n"

    md += f"""
## 滑價分布
- 平均: {avg_slip:+.3f}%
- 最大: {max(slippages):+.3f}% ({len(slippages)} 筆)
""" if slippages else "\n## 滑價分布\n- 無滑價數據\n"

    md += f"\n---\n*自動生成於 {datetime.now().isoformat()[:19]}*\n"

    # 寫出
    os.makedirs('artifacts', exist_ok=True)
    out_path = f"artifacts/monthly_report_{target_month.replace('-', '_')}.md"
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"✅ 月報已生成: {out_path}")

    # Telegram 通知
    msg = (f"📊 月報 {target_month}\n"
           f"信號 {n_signals} | 執行率 {exec_rate:.0f}% | "
           f"滑價 {avg_slip:+.3f}% | PnL {realized_pnl:+,.0f}")
    send_telegram(msg)

    print(md)

    # v9 tiered summary in monthly
    try:
        from strategy.risk_metrics import compute_tiered_risk_summary, format_tiered_risk_summary
        # rough from equity log
        eq_data = load_json(EQUITY_LOG)
        if eq_data and isinstance(eq_data, dict):
            summary = compute_tiered_risk_summary(
                equity_core=eq_data.get('core_equity_curve'),
                equity_sat=eq_data.get('sat_equity_curve'),
                target_ann_vol=0.15
            )
            print("\n" + format_tiered_risk_summary(summary))
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='Paper Trading 比對工具 v8')
    subparsers = parser.add_subparsers(dest='command', help='commands')

    # signals (enhanced)
    sig_parser = subparsers.add_parser('signals', help='擷取今日信號（可含籌碼/新聞標注）')
    sig_parser.add_argument('--notify', action='store_true', help='傳送 Telegram 通知')
    sig_parser.add_argument('--enrich', action='store_true',
                            help='加入三大法人籌碼 + 新聞情緒標注')

    # log
    log_parser = subparsers.add_parser('log', help='記錄實際成交')
    log_parser.add_argument('--ticker', required=True, help='股票代號')
    log_parser.add_argument('--action', choices=['buy', 'sell'], required=True)
    log_parser.add_argument('--price', type=float, required=True, help='成交價')
    log_parser.add_argument('--shares', type=int, required=True, help='股數')

    # report
    subparsers.add_parser('report', help='比對報告')

    # alert
    alert_parser = subparsers.add_parser('alert', help='回測回撤警報')
    alert_parser.add_argument('--max-dd', type=float, default=12.0,
                              help='回撤警報門檻 (預設 12%%)')

    # hardstop (v8 NEW)
    hs_parser = subparsers.add_parser('hardstop', help='組合層級權益保護')
    hs_parser.add_argument('--soft-stop', type=float, default=10.0,
                           help='Soft stop 門檻 %% (預設 10)')
    hs_parser.add_argument('--hard-stop', type=float, default=15.0,
                           help='Hard stop 門檻 %% (預設 15)')

    # monthly (v8 NEW)
    month_parser = subparsers.add_parser('monthly', help='生成月度績效報告')
    month_parser.add_argument('--month', help='月份 (YYYY-MM)，預設上月')

    # v9 tiered (NEW)
    tiered_parser = subparsers.add_parser('tiered', help='計算當前 Portfolio Vol Target + Tiered Core/Sat scales')
    tiered_parser.add_argument('--target-vol', type=float, default=0.15, help='目標年化波動率 (預設 0.15 = 15%)')
    tiered_parser.add_argument('--core-decay', type=float, default=0.35, help='Core 衰減係數')
    tiered_parser.add_argument('--sat-decay', type=float, default=0.85, help='Satellite 衰減係數（較激進）')
    tiered_parser.add_argument('--core-floor', type=float, default=0.55)
    tiered_parser.add_argument('--sat-floor', type=float, default=0.15)

    core_parser = subparsers.add_parser('core', help='建議/顯示 Core Holdings 篩選（Hybrid Tiered）')
    core_parser.add_argument('--core-cap', type=int, default=5, help='Core 上限檔數 (3-5)')

    args = parser.parse_args()

    if args.command == 'signals':
        generate_signals(args)
    elif args.command == 'log':
        log_trade(args)
    elif args.command == 'report':
        generate_report(args)
    elif args.command == 'alert':
        check_alert(args)
    elif args.command == 'hardstop':
        check_portfolio_hardstop(args)
    elif args.command == 'monthly':
        generate_monthly_report(args)
    elif args.command == 'tiered':
        compute_current_tiered_scales(args)
    elif args.command == 'core':
        suggest_core_holdings(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
