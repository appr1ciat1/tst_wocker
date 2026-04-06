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


TRADE_LOG = 'paper_trades.json'
SIGNAL_LOG = 'paper_signals.json'


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

    # 籌碼 + 新聞標注
    inst_data = {}
    news_data = {}
    if hasattr(args, 'enrich') and args.enrich:
        try:
            from strategy.institutional_flow import get_inst_flow_for_signals
            from strategy.news_sentiment import get_news_sentiment_for_signals
            all_tickers = [t for t, _, _, _ in matches]
            inst_data = get_inst_flow_for_signals(all_tickers)
            news_data = get_news_sentiment_for_signals(all_tickers)
        except Exception as e:
            print(f"   ⚠️ 籌碼/新聞標注失敗: {e}")

    has_enrich = bool(inst_data or news_data)

    print(f"📋 今日執行信號 ({today}):")
    header = f"{'股票':>6s} | {'進場價':>8s} | {'停利價':>8s} | {'停損價':>8s}"
    if has_enrich:
        header += f" | {'🏛️ 籌碼':>10s} | {'📰 新聞':>10s}"
    print(header)
    print("-" * (65 if has_enrich else 40))

    new_signals = []
    for ticker, entry, tp, sl in matches:
        line = f"{ticker:>6s} | {entry:>8s} | {tp:>8s} | {sl:>8s}"
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
                msg += f"<b>{s['ticker']}</b> 進場{s['entry_price']:.1f} TP{s['tp_price']:.1f} SL{s['sl_price']:.1f}"
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
        print("⚠️ 無成交記錄，請先用 paper_trade.py log 記錄成交")
        return

    soft_pct = args.soft_stop
    hard_pct = args.hard_stop

    # 計算已投入成本 vs 當前市值
    # 簡化模型：統計所有 buy 的成本，減掉所有 sell 的收入
    positions = {}  # {ticker: {'cost': float, 'shares': int}}
    total_invested = 0.0
    total_returned = 0.0

    for t in trades:
        ticker = t['ticker']
        if t['action'] == 'buy':
            if ticker not in positions:
                positions[ticker] = {'cost': 0.0, 'shares': 0}
            positions[ticker]['cost'] += t['price'] * t['shares']
            positions[ticker]['shares'] += t['shares']
            total_invested += t['price'] * t['shares']
        elif t['action'] == 'sell':
            total_returned += t['price'] * t['shares']
            if ticker in positions:
                positions[ticker]['shares'] -= t['shares']
                if positions[ticker]['shares'] <= 0:
                    del positions[ticker]

    # 未平倉成本
    open_cost = sum(p['cost'] for p in positions.values())
    realized_pnl = total_returned - (total_invested - open_cost)

    # 未實現 PnL 需要當前價格（從最新報表擷取或使用成本估算）
    print(f"🛡️ Portfolio Hard Stop 檢查")
    print(f"   累計投入:     {total_invested:,.0f}")
    print(f"   已回收:       {total_returned:,.0f}")
    print(f"   已實現 PnL:   {realized_pnl:+,.0f}")
    print(f"   未平倉檔數:   {len(positions)}")
    print(f"   Soft stop:    -{soft_pct:.0f}%")
    print(f"   Hard stop:    -{hard_pct:.0f}%")

    if total_invested > 0:
        pnl_pct = (realized_pnl / total_invested) * 100

        if abs(pnl_pct) > hard_pct:
            msg = (f"🚨🚨 HARD STOP 觸發！\n"
                   f"已實現損益: {pnl_pct:+.1f}%\n"
                   f"門檻: -{hard_pct:.0f}%\n"
                   f"⚡ 建議立即全部平倉")
            print(f"\n{msg}")
            send_telegram(msg)
            sys.exit(2)
        elif abs(pnl_pct) > soft_pct and pnl_pct < 0:
            msg = (f"⚠️ SOFT STOP 觸發！\n"
                   f"已實現損益: {pnl_pct:+.1f}%\n"
                   f"門檻: -{soft_pct:.0f}%\n"
                   f"💡 建議減半部位，暫停新進場")
            print(f"\n{msg}")
            send_telegram(msg)
            sys.exit(1)
        else:
            print(f"   已實現損益:   {pnl_pct:+.1f}%")
            print(f"   ✅ 在安全範圍內")


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
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
