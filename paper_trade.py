#!/usr/bin/env python3
"""
Paper Trading 比對工具

每日收盤後比對 AI 策略信號與實際成交紀錄，追蹤回測 vs 實盤偏差。

使用方式:
  # 1. 產出今日信號
  python paper_trade.py signals

  # 2. 產出信號 + LINE 通知
  python paper_trade.py signals --notify

  # 3. 記錄實際成交（手動輸入）
  python paper_trade.py log --ticker 2330 --action buy --price 980 --shares 1000

  # 4. 比對報告
  python paper_trade.py report

  # 5. 風險警報（檢查回撤）
  python paper_trade.py alert --max-dd 12

LINE Notify 設定:
  export LINE_NOTIFY_TOKEN='your_token_here'
  # 申請地址: https://notify-bot.line.me/my/
"""

import json
import os
import sys
from datetime import datetime, date
import argparse


TRADE_LOG = 'paper_trades.json'
SIGNAL_LOG = 'paper_signals.json'


def send_line_notify(message):
    """透過 LINE Notify 傳送通知。需設定環境變數 LINE_NOTIFY_TOKEN。"""
    token = os.environ.get('LINE_NOTIFY_TOKEN')
    if not token:
        return False
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({'message': message}).encode()
        req = urllib.request.Request(
            'https://notify-api.line.me/api/notify',
            data=data,
            headers={'Authorization': f'Bearer {token}'}
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'   ⚠️ LINE 通知失敗: {e}')
        return False


def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def generate_signals(args):
    """從最新 stock_report.html 擷取今日執行計畫。"""
    import re

    report_path = 'stock_report.html'
    if not os.path.exists(report_path):
        print("⚠️ stock_report.html 不存在，請先執行 python ai_report.py")
        return

    with open(report_path) as f:
        html = f.read()

    # 從 HTML 擷取「今日執行計畫」區塊
    # 搜尋交易計畫表格中的股票
    pattern = r'<td>(\d{4})</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>([\d\.]+)</td>\s*<td[^>]*>([\d\.]+)</td>\s*<td[^>]*>([\d\.]+)</td>'
    matches = re.findall(pattern, html)

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

    print(f"📋 今日執行信號 ({today}):")
    print(f"{'股票':>6s} | {'進場價':>8s} | {'停利價':>8s} | {'停損價':>8s}")
    print("-" * 40)

    new_signals = []
    for ticker, entry, tp, sl in matches:
        print(f"{ticker:>6s} | {entry:>8s} | {tp:>8s} | {sl:>8s}")
        new_signals.append({
            'date': today,
            'ticker': ticker,
            'entry_price': float(entry),
            'tp_price': float(tp),
            'sl_price': float(sl),
            'status': 'pending',
        })

    if new_signals:
        signals.extend(new_signals)
        save_json(SIGNAL_LOG, signals)
        print(f"\n✅ {len(new_signals)} 筆信號已記錄到 {SIGNAL_LOG}")

        # LINE 通知
        if hasattr(args, 'notify') and args.notify:
            msg = f"\n📊 今日執行信號 ({today})\n"
            for s in new_signals:
                msg += f"{s['ticker']} 進場{s['entry_price']:.1f} TP{s['tp_price']:.1f} SL{s['sl_price']:.1f}\n"
            if send_line_notify(msg):
                print("📤 已傳送 LINE 通知")
            else:
                print("⚠️ LINE 通知未設定（設定 LINE_NOTIFY_TOKEN 環境變數）")


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
        msg = f"\n🚨 風險警報！MDD -{mdd:.1f}% 已超過門檻 -{max_dd:.1f}%\n建議次月減半部位或暫停新倉"
        print(msg)
        if send_line_notify(msg):
            print("📤 已傳送 LINE 警報")
        sys.exit(1)
    else:
        print(f"   ✅ MDD 在安全範圍內")


def main():
    parser = argparse.ArgumentParser(description='Paper Trading 比對工具')
    subparsers = parser.add_subparsers(dest='command', help='commands')

    # signals
    sig_parser = subparsers.add_parser('signals', help='擷取今日信號')
    sig_parser.add_argument('--notify', action='store_true', help='傳送 LINE 通知')

    # log
    log_parser = subparsers.add_parser('log', help='記錄實際成交')
    log_parser.add_argument('--ticker', required=True, help='股票代號')
    log_parser.add_argument('--action', choices=['buy', 'sell'], required=True)
    log_parser.add_argument('--price', type=float, required=True, help='成交價')
    log_parser.add_argument('--shares', type=int, required=True, help='股數')

    # report
    subparsers.add_parser('report', help='比對報告')

    # alert
    alert_parser = subparsers.add_parser('alert', help='風險警報')
    alert_parser.add_argument('--max-dd', type=float, default=12.0,
                              help='回撤警報門檻 (預設 12%%)')

    args = parser.parse_args()

    if args.command == 'signals':
        generate_signals(args)
    elif args.command == 'log':
        log_trade(args)
    elif args.command == 'report':
        generate_report(args)
    elif args.command == 'alert':
        check_alert(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
