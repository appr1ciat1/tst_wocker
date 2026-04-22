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
import os
import re
import sys
from datetime import datetime, date, timedelta
import argparse

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

def get_current_prices(tickers):
    """用 yfinance 取得最新收盤價。"""
    import yfinance as yf
    prices = {}
    if not tickers:
        return prices
    symbols = [f"{t}.TW" for t in tickers]
    try:
        df = yf.download(symbols, period='2d', progress=False)
        close = df['Close'] if 'Close' in df.columns else df[('Close',)]
        if isinstance(close, (int, float)):
            # single ticker
            prices[tickers[0]] = float(close)
        else:
            for t, sym in zip(tickers, symbols):
                if sym in close.columns:
                    val = close[sym].dropna()
                    if len(val) > 0:
                        prices[t] = float(val.iloc[-1])
    except Exception as e:
        print(f"   ⚠️ 價格下載失敗: {e}")
    return prices

def extract_signals_from_report():
    """從 stock_report.html 擷取今日買入信號。"""
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
            })
    return signals

def update_tracker(data):
    """主要更新邏輯：追蹤持倉、結算已平倉、記錄新信號。"""
    today = date.today().isoformat()
    buy_cost_rate = 0.001425
    sell_cost_rate = 0.004425
    slippage = 0.001
    max_hold = 20
    position_size = 0.10

    print(f"📊 Paper Tracker 更新 ({today})")
    print(f"   初始資金: {data['initial_capital']:,.0f}")
    print(f"   當前現金: {data['capital']:,.0f}")
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
    prices = get_current_prices(list(all_tickers_set))

    # 2. 追蹤已持倉：檢查 TP/SL/時間到期
    to_close = []
    for ticker, pos in data['positions'].items():
        pos['day_count'] = pos.get('day_count', 0) + 1
        price = prices.get(ticker)
        if price is None:
            continue

        reason = None
        exit_price = price
        if price >= pos['tp']:
            reason = 'TP'
            exit_price = pos['tp']
        elif price <= pos['sl']:
            reason = 'SL'
            exit_price = pos['sl']
        elif pos['day_count'] >= max_hold:
            reason = 'TIME'
            exit_price = price

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
        opened = 0
        for sig in signals[:max_new]:
            ticker = sig['ticker']
            if ticker in data['positions']:
                continue
            entry_price = sig['entry']
            trade_amount = data['capital'] * position_size
            buy_cost = trade_amount * (buy_cost_rate + slippage)
            if data['capital'] >= trade_amount + buy_cost:
                shares = trade_amount / entry_price
                data['capital'] -= (trade_amount + buy_cost)
                data['positions'][ticker] = {
                    'entry': entry_price,
                    'tp': sig['tp'],
                    'sl': sig['sl'],
                    'entry_date': today,
                    'shares': round(shares, 0),
                    'day_count': 0,
                }
                opened += 1
                print(f"   🆕 開倉 {ticker} @ {entry_price:.1f} (TP {sig['tp']:.1f} / SL {sig['sl']:.1f})")
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
        起始日 {data['start_date']} | 更新 {today} | 初始資金 {initial:,.0f}
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
