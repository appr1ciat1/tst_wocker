#!/usr/bin/env python3
"""
Paper Trading 自動追蹤器 v8.5

每日收盤後執行，自動模擬 v8.5 策略的實盤績效：
1. 從 stock_report.html 擷取今日信號
2. 追蹤已持倉的 TP/SL/時間到期
3. 累積權益曲線到 paper_equity.json
4. 產出 paper_trading.html 績效網頁

使用方式:
  python paper_tracker.py              # 每日更新（GitHub Actions 自動執行）
  python paper_tracker.py --reset      # 清除所有記錄重新開始
"""

import json
import glob
import os
import re
import sys
from datetime import datetime, date, timedelta
import argparse
import pandas as pd

DATA_FILE = 'paper_equity.json'
HTML_FILE = 'paper_trading.html'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {
        'start_date': date.today().isoformat(),
        'initial_capital': 200_000,
        'capital': 200_000,
        'positions': {},          # {ticker: {entry, tp, sl, entry_date, shares, day_count}}
        'closed_trades': [],      # [{ticker, entry, exit, pnl_pct, reason, entry_date, exit_date}]
        'equity_curve': [],       # [{date, equity, capital, n_positions}]
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
            return yf.download(symbols, period='5d', progress=False)
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
    except Exception as e:
        print(f"   ⚠️ 價格下載失敗: {e}")
    return bars


def get_current_prices(tickers):
    """Backward-compatible latest close lookup."""
    return {ticker: bar['close'] for ticker, bar in get_current_bars(tickers).items()}


def extract_signals_from_orders():
    """從 artifacts/orders_YYYYMMDD.json 擷取今日機器可讀訂單。"""
    order_files = glob.glob('artifacts/orders_*.json')
    if not order_files:
        return []
    latest = max(order_files, key=os.path.getmtime)
    try:
        with open(latest, encoding='utf-8') as f:
            payload = json.load(f)
    except Exception as e:
        print(f"   ⚠️ orders JSON 讀取失敗: {e}")
        return []

    signals = []
    for order in payload.get('orders', []):
        if order.get('side') != 'buy':
            continue
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
        print(f"   📦 使用 orders JSON: {latest}")
    return signals

def extract_signals_from_report():
    """從 stock_report.html 擷取今日買入信號。"""
    order_signals = extract_signals_from_orders()
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
                'entry': float(entry_m[2]),  # third number is entry price (1st=ticker, 2nd=score, 3rd=price)
                'tp': float(tp_m.group(1)),
                'sl': float(sl_m.group(1)),
                'max_hold_days': 20,
            })
    return signals

def update_tracker(data):
    """主要更新邏輯：追蹤持倉、結算已平倉、記錄新信號。"""
    today = date.today().isoformat()
    buy_cost_rate = 0.001425
    sell_cost_rate = 0.004425
    slippage = 0.001
    max_hold = 20
    reserve_ratio = 0.10
    reserve_cash = data['initial_capital'] * reserve_ratio

    print(f"📊 Paper Tracker 更新 ({today})")
    print(f"   初始資金: {data['initial_capital']:,.0f}")
    print(f"   當前現金: {data['capital']:,.0f}")
    print(f"   保留現金: {reserve_cash:,.0f} ({reserve_ratio:.0%} 本金)")
    print(f"   持倉檔數: {len(data['positions'])}")

    # 0. 避免重複執行
    if data['equity_curve'] and data['equity_curve'][-1].get('date') == today:
        print(f"   ⚠️ 今日已更新過，跳過")
        return

    # 1. 取得所有相關股票的最新價格
    all_tickers = list(data['positions'].keys())
    signals = extract_signals_from_report()
    signal_tickers = [s['ticker'] for s in signals]
    all_tickers_set = set(all_tickers + signal_tickers)
    bars = get_current_bars(list(all_tickers_set))
    prices = {ticker: bar['close'] for ticker, bar in bars.items()}

    # 2. 追蹤已持倉：檢查 TP/SL/時間到期
    to_close = []
    for ticker, pos in data['positions'].items():
        pos['day_count'] = pos.get('day_count', 0) + 1
        bar = bars.get(ticker)
        if bar is None or bar.get('close') is None:
            continue

        reason = None
        exit_price = bar['close']
        pos_max_hold = pos.get('max_hold_days', max_hold)
        # Conservative same-day ordering: SL before TP, matching backtest.
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
            # 計算 PnL
            sell_cost = exit_price * pos['shares'] * sell_cost_rate
            slippage_cost = exit_price * pos['shares'] * slippage
            proceeds = exit_price * pos['shares'] - sell_cost - slippage_cost
            cost_basis = pos['entry'] * pos['shares'] * (1 + buy_cost_rate + slippage)
            pnl = proceeds - cost_basis
            pnl_pct = (exit_price / pos['entry'] - 1) * 100

            data['capital'] += proceeds
            data['closed_trades'].append({
                'ticker': ticker,
                'entry': pos['entry'],
                'exit': exit_price,
                'shares': pos['shares'],
                'pnl': round(pnl, 0),
                'pnl_pct': round(pnl_pct, 2),
                'reason': reason,
                'entry_date': pos['entry_date'],
                'exit_date': today,
                'days_held': pos['day_count'],
            })
            to_close.append(ticker)
            emoji = '🟢' if pnl > 0 else '🔴'
            print(f"   {emoji} 平倉 {ticker}: {pos['entry']:.1f}→{exit_price:.1f} ({pnl_pct:+.1f}%) [{reason}] 持{pos['day_count']}天")

    for t in to_close:
        del data['positions'][t]

    # 3. 記錄今日信號 & 開新倉
    if signals:
        data['daily_signals'].append({'date': today, 'tickers': signal_tickers})
        max_new = 7 - len(data['positions'])
        candidates = []
        opened = 0
        for sig in signals:
            if len(candidates) >= max_new:
                break
            ticker = sig['ticker']
            if ticker in data['positions']:
                continue
            execution_date = sig.get('execution_date')
            if execution_date and execution_date > today:
                continue
            bar = bars.get(ticker)
            if bar is None:
                continue
            limit_price = sig['entry']
            open_price = bar.get('open')
            low_price = bar.get('low')
            if low_price is not None and low_price > limit_price:
                print(f"   ⏭️ 未成交 {ticker}: low {low_price:.1f} > limit {limit_price:.1f}")
                continue
            entry_price = min(open_price, limit_price) if open_price is not None and open_price <= limit_price else limit_price
            candidates.append((sig, entry_price))

        for idx, (sig, entry_price) in enumerate(candidates):
            available_cash = max(data['capital'] - reserve_cash, 0)
            remaining_candidates = len(candidates) - idx
            if available_cash <= 0:
                print(f"   💵 保留本金 10%，可投入現金不足，停止開倉")
                break

            gross_budget = available_cash / remaining_candidates
            trade_amount = gross_budget / (1 + buy_cost_rate + slippage)
            shares = int(trade_amount / entry_price)
            if shares <= 0:
                print(f"   💵 資金不足 {sig['ticker']}: 無法在保留本金 10% 後買進")
                continue

            actual_trade_amount = shares * entry_price
            buy_cost = actual_trade_amount * (buy_cost_rate + slippage)
            if data['capital'] - actual_trade_amount - buy_cost < reserve_cash:
                print(f"   💵 資金不足 {sig['ticker']}: 保留本金 10% 後不開倉")
                continue

            ticker = sig['ticker']
            data['capital'] -= (actual_trade_amount + buy_cost)
            data['positions'][ticker] = {
                'entry': entry_price,
                'tp': sig['tp'],
                'sl': sig['sl'],
                'entry_date': today,
                'shares': shares,
                'day_count': 0,
                'max_hold_days': sig.get('max_hold_days', max_hold),
            }
            opened += 1
            print(
                f"   🆕 開倉 {ticker} @ {entry_price:.1f} × {shares:,.0f} "
                f"(投入 {actual_trade_amount:,.0f}, TP {sig['tp']:.1f} / SL {sig['sl']:.1f})"
            )
        if opened:
            print(f"   ✅ 今日開倉 {opened} 檔")
    else:
        print(f"   📋 今日無信號")

    # 4. 計算今日總權益
    total_equity = data['capital']
    for ticker, pos in data['positions'].items():
        price = prices.get(ticker, pos['entry'])
        total_equity += price * pos['shares']

    data['equity_curve'].append({
        'date': today,
        'equity': round(total_equity, 0),
        'capital': round(data['capital'], 0),
        'n_positions': len(data['positions']),
        'n_closed_today': len(to_close),
    })

    total_return = (total_equity / data['initial_capital'] - 1) * 100
    print(f"\n   💰 總權益: {total_equity:,.0f} ({total_return:+.1f}%)")
    print(f"   📈 已完成交易: {len(data['closed_trades'])} 筆")


def generate_html(data):
    """產出 paper trading 績效網頁。"""
    today = date.today().isoformat()
    initial = data['initial_capital']
    equity_curve = data['equity_curve']

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

    # 權益曲線 JSON
    dates_json = json.dumps([p['date'] for p in equity_curve])
    equity_json = json.dumps([p['equity'] for p in equity_curve])
    benchmark_json = json.dumps([initial] * len(equity_curve))

    # 交易清單 (最近 30 筆)
    recent_trades = trades[-30:][::-1]
    trades_html = ""
    for t in recent_trades:
        color = '#4ade80' if t['pnl'] > 0 else '#f87171'
        emoji = '🟢' if t['pnl'] > 0 else '🔴'
        trades_html += f"""
        <tr>
            <td>{t['exit_date']}</td>
            <td><b>{t['ticker']}</b></td>
            <td>{t['entry']:.1f}</td>
            <td>{t['exit']:.1f}</td>
            <td style="color:{color};font-weight:700">{t['pnl_pct']:+.1f}%</td>
            <td>{t['reason']}</td>
            <td>{t['days_held']}天</td>
        </tr>"""

    # 持倉
    positions_html = ""
    for ticker, pos in data['positions'].items():
        positions_html += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td>{pos['entry']:.1f}</td>
            <td>{pos['tp']:.1f}</td>
            <td>{pos['sl']:.1f}</td>
            <td>{pos['entry_date']}</td>
            <td>{pos.get('day_count', 0)}天</td>
        </tr>"""

    if not positions_html:
        positions_html = '<tr><td colspan="6" style="text-align:center;color:#888">目前無持倉</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Paper Trading v8.5 — {today}</title>
    <meta name="description" content="TW Stocker v8.5 Paper Trading 實時績效追蹤">
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
    <h1>📈 Paper Trading v8.5</h1>
    <p class="subtitle">
        <span class="badge badge-live">● LIVE</span>
        起始日 {data['start_date']} | 更新 {today} | 初始資金 {initial:,.0f} | 保留本金 10%，其餘投入
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
            <div class="label">年化報酬</div>
            <div class="value {'green' if ann_return > 0 else 'red'}">{ann_return:+.1f}%</div>
        </div>
        <div class="metric">
            <div class="label">最大回撤</div>
            <div class="value red">{mdd:.1f}%</div>
        </div>
        <div class="metric">
            <div class="label">勝率</div>
            <div class="value blue">{win_rate:.0f}%</div>
        </div>
        <div class="metric">
            <div class="label">交易數</div>
            <div class="value blue">{n_trades}</div>
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

    <div class="chart-box">
        <h2>權益曲線</h2>
        <canvas id="equityChart" height="80"></canvas>
    </div>

    <div class="chart-box">
        <h2>🔓 目前持倉</h2>
        <table>
            <tr><th>股票</th><th>進場價</th><th>停利</th><th>停損</th><th>進場日</th><th>持有</th></tr>
            {positions_html}
        </table>
    </div>

    <div class="chart-box">
        <h2>📋 近期交易（最近 30 筆）</h2>
        <table>
            <tr><th>日期</th><th>股票</th><th>進場</th><th>出場</th><th>損益</th><th>原因</th><th>持有</th></tr>
            {trades_html}
        </table>
    </div>

    <div class="disclaimer">
        ⚠️ <b>免責聲明：</b>此為 Paper Trading 模擬績效，非真實交易。歷史模擬不代表未來報酬。
        策略版本 v8.5 (Ablation-Proven)，含成本 0.58%/筆 + 10bps 滑價。投資有風險，決策請自行負責。
    </div>
</div>

<script>
const ctx = document.getElementById('equityChart').getContext('2d');
new Chart(ctx, {{
    type: 'line',
    data: {{
        labels: {dates_json},
        datasets: [{{
            label: 'Paper Trading 權益',
            data: {equity_json},
            borderColor: '#60a5fa',
            backgroundColor: 'rgba(96, 165, 250, 0.1)',
            fill: true,
            tension: 0.3,
            pointRadius: 2,
            borderWidth: 2,
        }}, {{
            label: '初始資金',
            data: {benchmark_json},
            borderColor: '#475569',
            borderDash: [5, 5],
            fill: false,
            pointRadius: 0,
            borderWidth: 1,
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ labels: {{ color: '#94a3b8' }} }},
        }},
        scales: {{
            x: {{ ticks: {{ color: '#64748b', maxTicksLimit: 10 }}, grid: {{ color: '#1e293b' }} }},
            y: {{ ticks: {{ color: '#64748b', callback: v => (v/1000).toFixed(0)+'K' }}, grid: {{ color: '#1e293b' }} }},
        }}
    }}
}});
</script>
</body>
</html>"""

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"   🌐 績效網頁已更新: {HTML_FILE}")


def main():
    parser = argparse.ArgumentParser(description='Paper Trading 自動追蹤器 v8.5')
    parser.add_argument('--reset', action='store_true', help='清除所有記錄重新開始')
    args = parser.parse_args()

    if args.reset:
        for f in [DATA_FILE, HTML_FILE]:
            if os.path.exists(f):
                os.remove(f)
        print("🔄 已清除所有 paper trading 記錄")
        return

    data = load_data()
    update_tracker(data)
    save_data(data)
    generate_html(data)
    print("✅ Paper Tracker 完成")


if __name__ == '__main__':
    main()
