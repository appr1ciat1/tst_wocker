#!/usr/bin/env python3
"""
AI 台股實戰區間交易系統 (Event-Driven Quantitative Trading Pipeline)

完整管線：資料下載 → AI 特徵排名 → 事件驅動回測 → HTML 報表產出

使用方式：
    python ai_report.py
    python ai_report.py --tickers 2330 2317 2454 --tp 0.15 --sl 0.08 --hold-days 20
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg')  # 無 GUI 模式（CI/CD 需要）
import matplotlib.pyplot as plt
import pandas as pd

# 確保 strategy/ 可被 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.ai_strategy import fetch_panel_data, engineer_features
from strategy.event_backtest import EventDrivenBacktester


# ==========================================
# 預設股池：熱門權值、AI、航運、金融股
# ==========================================
DEFAULT_TICKERS = [
    '2330', '2317', '2454', '2308', '2881',
    '2603', '3231', '3481', '2382', '2609',
    '2891', '1519', '2379', '2303',
]


def generate_report(trades_df, equity_df, total_score, close_df, config):
    """
    產出 AI 交易計畫 HTML 報表與資金曲線圖。

    Parameters
    ----------
    trades_df : pd.DataFrame
        回測交易明細
    equity_df : pd.DataFrame
        每日資金曲線
    total_score : pd.DataFrame
        AI 評分矩陣
    close_df : pd.DataFrame
        收盤價矩陣
    config : dict
        策略參數 (tp_pct, sl_pct, max_hold_days, initial_capital)
    """
    print("📊 產出 AI 交易計畫與績效報表...")

    tp_pct = config['tp_pct']
    sl_pct = config['sl_pct']
    max_hold_days = config['max_hold_days']
    initial_capital = config['initial_capital']

    # === 績效統計 ===
    total_ret = (equity_df['Equity'].iloc[-1] / initial_capital - 1) * 100

    if not trades_df.empty:
        total_trades = len(trades_df)
        win_rate = len(trades_df[trades_df['Return_Pct'] > 0]) / total_trades * 100
        avg_return = trades_df['Return_Pct'].mean() * 100

        # 出場原因統計
        reason_counts = trades_df['Reason'].value_counts().to_dict()
    else:
        total_trades, win_rate, avg_return = 0, 0, 0
        reason_counts = {}

    # === 繪製資金曲線 ===
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(equity_df.index, equity_df['Equity'], color='#00e5ff', lw=2, label='Strategy Equity')
    ax.axhline(initial_capital, color='#555', linestyle='--', alpha=0.7, label='Initial Capital')
    ax.fill_between(equity_df.index, initial_capital, equity_df['Equity'],
                     where=equity_df['Equity'] >= initial_capital, alpha=0.15, color='#00e5ff')
    ax.fill_between(equity_df.index, initial_capital, equity_df['Equity'],
                     where=equity_df['Equity'] < initial_capital, alpha=0.15, color='#ff4444')
    ax.set_title(f'AI Quant Backtest  |  TP: +{tp_pct*100:.0f}%  SL: -{sl_pct*100:.0f}%  Hold: {max_hold_days}D',
                 fontweight='bold', fontsize=14, color='#fff')
    ax.set_ylabel('Portfolio Value (TWD)', fontsize=11)
    ax.legend(fontsize=10)
    ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig('backtest_chart.png', dpi=150, bbox_inches='tight', facecolor='#121212')
    plt.close(fig)
    print("   📈 資金曲線已存為 backtest_chart.png")

    # === 今日交易計畫 ===
    latest_date = total_score.index[-1]
    today_scores = total_score.loc[latest_date].sort_values(ascending=False)
    threshold = config.get('threshold', 3.2)

    trading_plan_rows = ""
    for ticker, score in today_scores.head(15).items():
        price = close_df[ticker].iloc[-1]
        if pd.isna(price):
            continue

        if score >= threshold:
            tp_price = price * (1 + tp_pct)
            sl_price = price * (1 - sl_pct)
            time_exit_date = (latest_date + timedelta(days=int(max_hold_days * 1.4))).strftime('%Y-%m-%d')
            status = '<span style="color:#00ff00; font-weight:bold;">🟢 建議買進</span>'
            plan = (f'<b>停利:</b> <span style="color:#00ff00">{tp_price:.1f}</span> (+{tp_pct*100:.0f}%) '
                    f'<br><b>停損:</b> <span style="color:#ff4444">{sl_price:.1f}</span> (-{sl_pct*100:.0f}%) '
                    f'<br><b>最晚出場:</b> {time_exit_date}')
        else:
            status = '<span style="color:#aaaaaa">⚪ 觀望</span>'
            plan = "-"

        trading_plan_rows += (
            f'<tr><td>{ticker}</td><td>{score:.2f}</td>'
            f'<td>{price:.1f}</td><td>{status}</td><td>{plan}</td></tr>\n'
        )

    # === 歷史交易紀錄（最近 15 筆）===
    trade_history_rows = ""
    if not trades_df.empty:
        for _, row in trades_df.tail(15).iloc[::-1].iterrows():
            color = "#00ff00" if row['Return_Pct'] > 0 else "#ff4444"
            trade_history_rows += (
                f'<tr>'
                f'<td>{row["Ticker"]}</td>'
                f'<td>{row["Entry_Date"]}</td>'
                f'<td>{row["Exit_Date"]}</td>'
                f'<td>{row["Entry_Price"]:.1f}</td>'
                f'<td>{row["Exit_Price"]:.1f}</td>'
                f'<td>{row["Reason"]}</td>'
                f'<td>{row["Days_Held"]}天</td>'
                f'<td style="color:{color}; font-weight:bold;">{row["Return_Pct"]*100:+.1f}%</td>'
                f'</tr>\n'
            )

    # === 出場原因統計 ===
    reason_stats_rows = ""
    for reason, count in reason_counts.items():
        subset = trades_df[trades_df['Reason'] == reason]
        avg_ret = subset['Return_Pct'].mean() * 100
        reason_stats_rows += (
            f'<tr><td>{reason}</td><td>{count} 筆</td>'
            f'<td style="color:{"#00ff00" if avg_ret > 0 else "#ff4444"}">{avg_ret:+.2f}%</td></tr>\n'
        )

    # === 產出 HTML ===
    report_date = latest_date.strftime('%Y-%m-%d')
    total_ret_color = "#00ff00" if total_ret > 0 else "#ff4444"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI 台股區間交易計畫 — {report_date}</title>
    <meta name="description" content="AI 驅動的台股量化交易系統，提供每日 OCO 智慧掛單建議與精確回測績效">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: #0a0a0f;
            color: #e0e0e0;
            padding: 24px;
            line-height: 1.6;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{
            font-size: 1.8rem;
            color: #00e5ff;
            border-bottom: 2px solid #1a1a2e;
            padding-bottom: 12px;
            margin-bottom: 8px;
        }}
        h2 {{
            font-size: 1.3rem;
            color: #00e5ff;
            margin-top: 32px;
            margin-bottom: 12px;
            padding-bottom: 6px;
            border-bottom: 1px solid #1a1a2e;
        }}
        .subtitle {{
            color: #888;
            font-size: 0.9rem;
            margin-bottom: 24px;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 28px;
        }}
        .stat-card {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 20px;
            border-radius: 12px;
            text-align: center;
            border-left: 4px solid #00e5ff;
        }}
        .stat-card .value {{
            font-size: 1.8rem;
            font-weight: 700;
            margin: 4px 0;
        }}
        .stat-card .label {{
            font-size: 0.8rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 8px;
            background: #1a1a2e;
            border-radius: 8px;
            overflow: hidden;
        }}
        th, td {{
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid #252540;
            font-size: 0.88rem;
        }}
        th {{
            background: #16213e;
            color: #00e5ff;
            font-weight: 600;
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }}
        tr:hover {{ background: #16213e; }}
        img {{
            max-width: 100%;
            border-radius: 10px;
            margin-top: 12px;
            border: 1px solid #252540;
        }}
        .disclaimer {{
            margin-top: 40px;
            padding: 16px;
            background: #1a1a0e;
            border-left: 4px solid #ffab00;
            border-radius: 8px;
            font-size: 0.82rem;
            color: #999;
        }}
        .tag {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }}
    </style>
</head>
<body>
<div class="container">

    <h1>🎯 AI 台股實戰區間交易計畫</h1>
    <p class="subtitle">
        Event-Driven System &nbsp;|&nbsp; 報表日期: {report_date} &nbsp;|&nbsp;
        🛡️ 停利 +{tp_pct*100:.0f}% &nbsp; 停損 -{sl_pct*100:.0f}% &nbsp; 最長持有 {max_hold_days} 天
    </p>

    <div class="stats">
        <div class="stat-card">
            <div class="label">策略總報酬率</div>
            <div class="value" style="color:{total_ret_color};">{total_ret:+.1f}%</div>
        </div>
        <div class="stat-card">
            <div class="label">完成交易次數</div>
            <div class="value">{total_trades}</div>
        </div>
        <div class="stat-card">
            <div class="label">真實回測勝率</div>
            <div class="value" style="color:#00ff00;">{win_rate:.1f}%</div>
        </div>
        <div class="stat-card">
            <div class="label">單筆平均期望值</div>
            <div class="value">{avg_return:+.2f}%</div>
        </div>
    </div>

    <h2>🚀 今日 AI 交易執行單 (建議掛 OCO 智慧單)</h2>
    <table>
        <thead>
            <tr>
                <th>股票代號</th>
                <th>AI 評分</th>
                <th>今日收盤</th>
                <th>操作狀態</th>
                <th>🎯 區間執行計畫 (停利 / 停損 / 時間)</th>
            </tr>
        </thead>
        <tbody>
{trading_plan_rows}
        </tbody>
    </table>

    <h2>📊 真實資金曲線 (含停損停利與資金控管)</h2>
    <img src="backtest_chart.png" alt="AI Quantitative Backtest Equity Curve">

    <h2>📋 出場原因分布統計</h2>
    <table>
        <thead><tr><th>出場原因</th><th>次數</th><th>平均報酬</th></tr></thead>
        <tbody>
{reason_stats_rows}
        </tbody>
    </table>

    <h2>📜 最近交易紀錄 (最新 15 筆)</h2>
    <table>
        <thead>
            <tr>
                <th>股票</th><th>進場日</th><th>出場日</th>
                <th>進場價</th><th>出場價</th>
                <th>觸發原因</th><th>持有天數</th><th>報酬率</th>
            </tr>
        </thead>
        <tbody>
{trade_history_rows}
        </tbody>
    </table>

    <div class="disclaimer">
        ⚠️ <b>免責聲明：</b>本報表由 AI 量化模型自動產出，僅供學術研究與技術交流之用，
        不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
    </div>

</div>
</body>
</html>"""

    with open('stock_report.html', 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"   ✅ 報表已生成：stock_report.html")


def parse_args():
    """解析命令列參數。"""
    parser = argparse.ArgumentParser(
        description='AI 台股區間交易系統 — 事件驅動回測與交易計畫產生器'
    )
    parser.add_argument(
        '--tickers', nargs='+', default=DEFAULT_TICKERS,
        help='股池代號列表 (預設: 14 檔熱門股)'
    )
    parser.add_argument(
        '--tp', type=float, default=0.15,
        help='停利百分比 (預設: 0.15 = +15%%)'
    )
    parser.add_argument(
        '--sl', type=float, default=0.08,
        help='停損百分比 (預設: 0.08 = -8%%)'
    )
    parser.add_argument(
        '--hold-days', type=int, default=20,
        help='最大持倉交易日數 (預設: 20)'
    )
    parser.add_argument(
        '--days', type=int, default=800,
        help='歷史回測天數 (預設: 800)'
    )
    parser.add_argument(
        '--threshold', type=float, default=3.2,
        help='AI 評分進場門檻 (預設: 3.2，滿分 4.0)'
    )
    parser.add_argument(
        '--capital', type=float, default=1_000_000,
        help='初始模擬資金 (預設: 1000000)'
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("🎯 AI 台股實戰區間交易系統 v2.0")
    print("=" * 60)
    print(f"   股池: {', '.join(args.tickers)}")
    print(f"   停利: +{args.tp*100:.0f}%  停損: -{args.sl*100:.0f}%  "
          f"持倉上限: {args.hold_days} 天")
    print(f"   回測天數: {args.days}  進場門檻: {args.threshold}")
    print("=" * 60)

    # Phase 1: 資料下載
    close_df, high_df, low_df, vol_df = fetch_panel_data(args.tickers, days=args.days)

    # Phase 2: 特徵工程
    total_score, ma_60 = engineer_features(close_df, vol_df)

    # Phase 3 & 4: 事件驅動回測
    backtester = EventDrivenBacktester(
        tp_pct=args.tp,
        sl_pct=args.sl,
        max_hold_days=args.hold_days,
        initial_capital=args.capital,
        position_size=0.10,
    )
    trades_df, equity_df = backtester.run(
        total_score, close_df, high_df, low_df, ma_60,
        threshold=args.threshold,
    )

    # Phase 5: 報表產出
    config = {
        'tp_pct': args.tp,
        'sl_pct': args.sl,
        'max_hold_days': args.hold_days,
        'initial_capital': args.capital,
        'threshold': args.threshold,
    }
    generate_report(trades_df, equity_df, total_score, close_df, config)
    print("\n🚀 全部完成！請打開 stock_report.html 查看結果。")


if __name__ == '__main__':
    main()
