#!/usr/bin/env python3
"""
因子 Ablation Study — 分析各特徵對策略績效的邊際貢獻

跑 6 組因子組合 + 3 組持倉天數變體的完整 ablation，
輸出標準化比較表 (ablation_results.csv) 和對比圖 (ablation_chart.png)。

使用方式：
    python ablation_study.py
    python ablation_study.py --tickers 2330 2317 2454 --days 800
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from itertools import combinations

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.ai_strategy import fetch_panel_data, build_liquid_universe
from strategy.event_backtest import EventDrivenBacktester
from strategy.risk_metrics import compute_risk_metrics


DEFAULT_TICKERS = [
    '2330', '2317', '2454', '2308', '2881',
    '2603', '3231', '3481', '2382', '2609',
    '2891', '1519', '2379', '2303',
]


def compute_factor_scores(close_df, vol_df, factors, universe_mask=None):
    """
    根據指定的因子子集計算評分。

    Parameters
    ----------
    close_df, vol_df : pd.DataFrame
    factors : list[str]
        因子名稱列表，可選 'momentum', 'trend', 'volume', 'stability'
    universe_mask : pd.DataFrame, optional

    Returns
    -------
    total_score, ma_60, atr_df
    """
    # 計算所有原始指標
    mom_20 = close_df / close_df.shift(20)
    ma_60 = close_df.rolling(60).mean()
    trend_bias = close_df / ma_60
    vol_surge = vol_df.rolling(5).mean() / (vol_df.rolling(20).mean() + 1e-8)
    volatility = close_df.pct_change().rolling(20).std()
    stability = 1 / (volatility + 1e-8)

    factor_map = {
        'momentum': mom_20,
        'trend': trend_bias,
        'volume': vol_surge,
        'stability': stability,
    }

    # 只對選定因子做排名並加總
    score_parts = []
    for name in factors:
        raw = factor_map[name]
        if universe_mask is not None:
            raw = raw.where(universe_mask)
        ranked = raw.rank(axis=1, pct=True)
        score_parts.append(ranked)

    total_score = sum(score_parts) if score_parts else pd.DataFrame(0, index=close_df.index, columns=close_df.columns)

    atr_df = close_df.pct_change().abs().rolling(20).mean() * close_df

    return total_score, ma_60, atr_df


def run_single_ablation(label, factors, close_df, open_df, high_df, low_df, vol_df,
                        universe_mask, hold_days, initial_capital, top_k, threshold):
    """跑一組 ablation 並回傳績效指標。"""
    print(f"\n--- Ablation: {label} ({', '.join(factors) if factors else 'NONE'}) ---")

    total_score, ma_60, atr_df = compute_factor_scores(close_df, vol_df, factors, universe_mask)

    # 調整 threshold：因子數量不同，滿分也不同
    # 原始滿分 4.0 對應 threshold 2.0 → 比例 = 0.5
    adjusted_threshold = len(factors) * 0.5 if factors else 0

    backtester = EventDrivenBacktester(
        tp_pct=0.15,
        sl_pct=0.08,
        max_hold_days=hold_days,
        initial_capital=initial_capital,
        position_size=0.10,
        tp_sl_mode='atr',
        tp_atr_mult=3.0,
        sl_atr_mult=1.5,
        buy_cost=0.001425,
        sell_cost=0.004425,
    )

    trades_df, equity_df = backtester.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=top_k,
        threshold=adjusted_threshold,
    )

    metrics = compute_risk_metrics(equity_df, trades_df, initial_capital)
    metrics['label'] = label
    metrics['factors'] = ', '.join(factors) if factors else 'NONE'
    metrics['hold_days'] = hold_days

    return metrics, equity_df


def main():
    parser = argparse.ArgumentParser(description='因子 Ablation Study')
    parser.add_argument('--tickers', nargs='+', default=DEFAULT_TICKERS)
    parser.add_argument('--days', type=int, default=800)
    parser.add_argument('--capital', type=float, default=1_000_000)
    parser.add_argument('--top-k', type=int, default=3)
    parser.add_argument('--threshold', type=float, default=2.0)
    args = parser.parse_args()

    print("=" * 60)
    print("🔬 因子 Ablation Study")
    print("=" * 60)

    # 下載資料
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(args.tickers, days=args.days)
    universe_mask = None  # ablation 在靜態池中跑

    # === 因子組合實驗 ===
    all_factors = ['momentum', 'trend', 'volume', 'stability']

    ablation_configs = [
        ('All 4 factors', all_factors, 20),
        ('No volume', ['momentum', 'trend', 'stability'], 20),
        ('No stability', ['momentum', 'trend', 'volume'], 20),
        ('Mom + Trend only', ['momentum', 'trend'], 20),
        ('Momentum only', ['momentum'], 20),
        ('Trend only', ['trend'], 20),
    ]

    # === 持倉天數實驗 ===
    for hd in [10, 20, 30]:
        if hd != 20:  # 20 天已在上面
            ablation_configs.append((f'All 4, Hold={hd}D', all_factors, hd))

    # 執行所有 ablation
    results = []
    equity_curves = {}

    for label, factors, hold_days in ablation_configs:
        metrics, eq_df = run_single_ablation(
            label, factors, close_df, open_df, high_df, low_df, vol_df,
            universe_mask, hold_days, args.capital, args.top_k, args.threshold,
        )
        results.append(metrics)
        equity_curves[label] = eq_df

    # === 輸出比較表 ===
    summary_rows = []
    for m in results:
        summary_rows.append({
            'Configuration': m['label'],
            'Factors': m['factors'],
            'Hold_Days': m['hold_days'],
            'Total_Return_%': round(m['total_return'] * 100, 2),
            'Ann_Return_%': round(m['ann_return'] * 100, 2),
            'Ann_Vol_%': round(m['ann_volatility'] * 100, 2),
            'Sharpe': round(m['sharpe'], 3),
            'Sortino': round(m['sortino'], 3),
            'Max_DD_%': round(m['max_drawdown_pct'] * 100, 1),
            'Calmar': round(m['calmar'], 3),
            'Trades': m['total_trades'],
            'Win_Rate_%': round(m['win_rate'] * 100, 1),
            'Profit_Factor': round(m['profit_factor'], 2),
            'Avg_Return_%': round(m['avg_return'] * 100, 2),
        })

    results_df = pd.DataFrame(summary_rows)
    results_df.to_csv('ablation_results.csv', index=False)
    print("\n\n" + "=" * 80)
    print("📊 Ablation Study Results")
    print("=" * 80)
    print(results_df.to_string(index=False))
    print(f"\n✅ 結果已存為 ablation_results.csv")

    # === 繪製對比圖 ===
    plt.style.use('dark_background')
    fig, axes = plt.subplots(1, 2, figsize=(18, 7))

    # 左圖：因子組合的資金曲線
    ax1 = axes[0]
    factor_labels = [c[0] for c in ablation_configs if c[2] == 20]
    colors = ['#00e5ff', '#ffab00', '#ab47bc', '#ff4444', '#00ff00', '#ff9800']
    for i, label in enumerate(factor_labels):
        if label in equity_curves:
            eq = equity_curves[label]
            ax1.plot(eq.index, eq['Equity'], lw=1.8, label=label,
                     color=colors[i % len(colors)])
    ax1.axhline(args.capital, color='#555', ls='--', alpha=0.5)
    ax1.set_title('Factor Ablation (Hold=20D)', fontweight='bold', fontsize=13, color='#fff')
    ax1.set_ylabel('Portfolio Value (TWD)')
    ax1.legend(fontsize=8, loc='upper left')
    ax1.grid(alpha=0.15)

    # 右圖：Sharpe 比較 bar chart
    ax2 = axes[1]
    sharpes = [m['sharpe'] for m in results]
    labels = [m['label'] for m in results]
    bar_colors = ['#00e5ff' if s > 0 else '#ff4444' for s in sharpes]
    bars = ax2.barh(range(len(labels)), sharpes, color=bar_colors, alpha=0.8)
    ax2.set_yticks(range(len(labels)))
    ax2.set_yticklabels(labels, fontsize=9)
    ax2.set_xlabel('Sharpe Ratio')
    ax2.set_title('Sharpe Ratio Comparison', fontweight='bold', fontsize=13, color='#fff')
    ax2.axvline(0, color='#555', ls='--', alpha=0.5)
    ax2.grid(alpha=0.15, axis='x')

    fig.tight_layout()
    fig.savefig('ablation_chart.png', dpi=150, bbox_inches='tight', facecolor='#121212')
    plt.close(fig)
    print(f"📈 對比圖已存為 ablation_chart.png")


if __name__ == '__main__':
    main()
