#!/usr/bin/env python3
"""
Monte Carlo 壓力測試 v3 — Equity-Curve Bootstrap

與 v2 的差異：
- v2 對「單筆交易報酬」做 bootstrap，然後用固定 10% 倉位順序乘上去。
  這丟掉了多檔同持、regime 曝險縮放、gap sizing、資金占用等所有組合效應。
- v3 對「每日組合報酬率」做 block bootstrap，天然保留了上述所有效應，
  因為每日報酬率已經是完整回測引擎跑出來的結果。

使用方式:
  python monte_carlo.py --equity artifacts/equity_YYYYMMDD.csv
  python monte_carlo.py --runs 5000          # 更精確
  python monte_carlo.py --block-size 10      # 較短區塊
  python monte_carlo.py --seed 7             # 固定抽樣 seed
  python monte_carlo.py --legacy             # 舊版單筆 bootstrap (保留做比較)
"""

import re
import sys
import argparse
import random
import statistics
import csv
import os
import glob
from datetime import datetime

import pandas as pd
import numpy as np


def get_equity_curve(equity_path):
    """從指定 equity CSV 取得每日組合權益曲線。"""
    if not equity_path:
        raise ValueError("請用 --equity 明確指定 artifacts/equity_*.csv")
    if not os.path.exists(equity_path):
        raise FileNotFoundError(f"找不到 equity CSV: {equity_path}")

    print(f"   讀取 {equity_path}...")
    df = pd.read_csv(equity_path, index_col=0, parse_dates=True)
    if 'Equity' not in df.columns:
        raise ValueError(f"{equity_path} 缺少 Equity 欄位")
    return df['Equity']


def get_trades(trades_path=None):
    """從最新 trades CSV 取得交易列表（legacy 模式用）。"""
    if trades_path:
        latest = trades_path
    else:
        csv_files = glob.glob('artifacts/trades_*.csv')
        if not csv_files:
            return []
        latest = max(csv_files, key=os.path.getmtime)
    if not os.path.exists(latest):
        raise FileNotFoundError(f"找不到 trades CSV: {latest}")
    trades = []
    with open(latest) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trades.append({
                    'return': float(row['Return_Pct']),
                    'entry_date': row.get('Entry_Date', ''),
                    'reason': row.get('Reason', ''),
                })
            except (KeyError, ValueError):
                continue
    return trades


def equity_curve_bootstrap(daily_returns, n_runs, block_size=20,
                           initial=1_000_000):
    """
    Block bootstrap 每日組合報酬率，模擬權益曲線分布。

    這保留了回測引擎的所有組合效應（多檔同持、regime、gap sizing 等），
    因為每日報酬率已經是這些效應的結果。

    Parameters
    ----------
    daily_returns : pd.Series or np.array
        每日組合報酬率 (decimal, e.g. 0.01 = +1%)
    n_runs : int
        模擬次數
    block_size : int
        Block bootstrap 區塊大小 (天)
    initial : float
        初始資金

    Returns
    -------
    dict with distribution statistics
    """
    rets = np.array(daily_returns)
    n = len(rets)
    all_total_returns = []
    all_mdds = []
    all_sharpes = []

    for _ in range(n_runs):
        # Block bootstrap: 隨機取 block_size 天的連續區塊，拼接到原長度
        sample = []
        while len(sample) < n:
            start = random.randint(0, max(0, n - block_size))
            sample.extend(rets[start:start + block_size])
        sample = np.array(sample[:n])

        # 計算模擬權益曲線
        equity = initial * np.cumprod(1 + sample)
        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / peak

        total_ret = equity[-1] / initial - 1
        max_dd = drawdown.min()

        # 模擬 Sharpe
        sim_mean = sample.mean()
        sim_std = sample.std()
        if sim_std > 0:
            sharpe = sim_mean / sim_std * np.sqrt(252)
        else:
            sharpe = 0

        all_total_returns.append(total_ret)
        all_mdds.append(max_dd)
        all_sharpes.append(sharpe)

    all_total_returns.sort()
    all_mdds.sort()
    all_sharpes.sort()

    return {
        'n_days': n,
        'median_ret': np.median(all_total_returns),
        'p5_ret': all_total_returns[int(n_runs * 0.05)],
        'p25_ret': all_total_returns[int(n_runs * 0.25)],
        'p75_ret': all_total_returns[int(n_runs * 0.75)],
        'p95_ret': all_total_returns[int(n_runs * 0.95)],
        'median_mdd': np.median(all_mdds),
        'p5_mdd': all_mdds[int(n_runs * 0.05)],  # worst 5%
        'p95_mdd': all_mdds[int(n_runs * 0.95)],  # best 5%
        'median_sharpe': np.median(all_sharpes),
        'p5_sharpe': all_sharpes[int(n_runs * 0.05)],
    }


def legacy_simulate(returns, n_runs, block_size=5, initial=1_000_000,
                    position_size=0.10):
    """
    Legacy 模式：對單筆交易報酬做 bootstrap。

    ⚠️ 這個模式有方法論缺陷（丟掉了組合效應），保留僅供對比。
    """
    n = len(returns)
    all_returns = []
    all_mdds = []

    for _ in range(n_runs):
        if block_size <= 1:
            sample = random.choices(returns, k=n)
        else:
            sample = []
            while len(sample) < n:
                start = random.randint(0, max(0, n - block_size))
                sample.extend(returns[start:start + block_size])
            sample = sample[:n]

        equity = initial
        peak = initial
        max_dd = 0
        for ret in sample:
            equity += equity * position_size * ret
            peak = max(peak, equity)
            dd = (equity - peak) / peak
            max_dd = min(max_dd, dd)

        all_returns.append(equity / initial - 1)
        all_mdds.append(max_dd)

    all_returns.sort()
    all_mdds.sort()

    return {
        'n_trades': n,
        'median_ret': statistics.median(all_returns),
        'p5_ret': all_returns[int(n_runs * 0.05)],
        'p5_mdd': all_mdds[int(n_runs * 0.05)],
        'median_mdd': statistics.median(all_mdds),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Monte Carlo 壓力測試 v3 — Equity-Curve Bootstrap'
    )
    parser.add_argument('--runs', type=int, default=2000,
                        help='模擬次數 (預設 2000)')
    parser.add_argument('--block-size', type=int, default=20,
                        help='Block bootstrap 區塊大小-天 (預設 20 ≈ 1 個月)')
    parser.add_argument('--legacy', action='store_true',
                        help='也跑 legacy 單筆 bootstrap 做對比')
    parser.add_argument('--equity', type=str, required=True,
                        help='明確指定 equity CSV，例如 artifacts/equity_20260525.csv')
    parser.add_argument('--trades', type=str, default=None,
                        help='legacy 模式使用的 trades CSV；未提供則讀取最新 trades CSV')
    parser.add_argument('--seed', type=int, default=42,
                        help='隨機種子，讓 bootstrap 結果可重現 (預設: 42)')
    args = parser.parse_args()

    random.seed(args.seed)

    # === Equity-Curve Bootstrap ===
    equity = get_equity_curve(args.equity)
    daily_returns = equity.pct_change().dropna()
    n_days = len(daily_returns)

    # 原始績效
    orig_total_ret = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    orig_peak = equity.cummax()
    orig_mdd = ((equity - orig_peak) / orig_peak).min() * 100
    orig_sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

    print(f"\n{'='*70}")
    print(f"📊 Monte Carlo 壓力測試 v3 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}")
    print(f"   方法：Equity-Curve Block Bootstrap")
    print(f"   輸入：{args.equity}")
    print(f"   資料：{n_days} 個交易日的每日組合報酬率")
    print(f"   區塊大小：{args.block_size} 天 (保留時序自相關)")
    print(f"   模擬次數：{args.runs}")
    print(f"   Seed：{args.seed}")
    print()
    print(f"   ⚠️  方法論說明：")
    print(f"      本工具對已完成回測的「每日組合報酬率」做 block bootstrap。")
    print(f"      每日報酬率已包含多檔同持、regime 曝險縮放、gap-aware sizing")
    print(f"      等所有組合效應。但 bootstrap 仍假設日報酬的時序結構")
    print(f"      可以被隨機重排，這在極端 regime 轉換時可能不成立。")
    print()

    print(f"📈 原始回測結果:")
    print(f"   總報酬: {orig_total_ret:+.1f}%  MDD: {orig_mdd:.1f}%  "
          f"Sharpe: {orig_sharpe:.2f}")

    # Run equity-curve bootstrap
    print(f"\n🎲 Equity-Curve Bootstrap ({args.runs} 次)...")
    ec_stats = equity_curve_bootstrap(
        daily_returns, args.runs,
        block_size=args.block_size,
        initial=float(equity.iloc[0]),
    )

    print(f"\n{'='*70}")
    print(f"{'指標':<20s} | {'最差5%':>10s} | {'25%':>10s} | "
          f"{'中位數':>10s} | {'75%':>10s} | {'最好5%':>10s}")
    print(f"{'-'*70}")
    print(f"{'總報酬':<20s} | {ec_stats['p5_ret']*100:>+9.1f}% | "
          f"{ec_stats['p25_ret']*100:>+9.1f}% | {ec_stats['median_ret']*100:>+9.1f}% | "
          f"{ec_stats['p75_ret']*100:>+9.1f}% | {ec_stats['p95_ret']*100:>+9.1f}%")
    print(f"{'MDD':<20s} | {ec_stats['p5_mdd']*100:>9.1f}% | "
          f"{'—':>10s} | {ec_stats['median_mdd']*100:>9.1f}% | "
          f"{'—':>10s} | {ec_stats['p95_mdd']*100:>9.1f}%")
    print(f"{'Sharpe (年化)':<20s} | {ec_stats['p5_sharpe']:>10.2f} | "
          f"{'—':>10s} | {ec_stats['median_sharpe']:>10.2f} | "
          f"{'—':>10s} | {'—':>10s}")
    print(f"{'='*70}")

    # Risk assessment
    print(f"\n📊 風險評估:")
    worst_mdd = ec_stats['p5_mdd']
    worst_ret = ec_stats['p5_ret']
    median_sharpe = ec_stats['median_sharpe']

    if worst_ret > 0:
        print(f"   ✅ 最差 5% 總報酬仍為正 ({worst_ret*100:+.1f}%)")
    else:
        print(f"   ⚠️  最差 5% 總報酬為負 ({worst_ret*100:+.1f}%)")

    if abs(worst_mdd) < 0.25:
        print(f"   ✅ 最差 5% MDD = {worst_mdd*100:.1f}% (優於 -25%)")
    elif abs(worst_mdd) < 0.40:
        print(f"   ⚠️  最差 5% MDD = {worst_mdd*100:.1f}% (介於 -25% ~ -40%)")
    else:
        print(f"   🚨 最差 5% MDD = {worst_mdd*100:.1f}% (低於 -40%，嚴重風險)")

    print(f"   {'✅' if median_sharpe > 1.5 else '⚠️'} "
          f"中位數 Sharpe = {median_sharpe:.2f}")

    # Practical suggestions
    print(f"\n💡 實盤建議:")
    suggested_capital = 100_000 / abs(worst_mdd) if worst_mdd != 0 else 1_000_000
    print(f"   預期 MDD 範圍: {ec_stats['median_mdd']*100:.1f}% ~ {worst_mdd*100:.1f}%")
    print(f"   建議起始資金:  ≥ {suggested_capital:,.0f} 元")
    print(f"   (以「最差 5% MDD 時仍保有 10 萬緩衝」計算)")

    # === Legacy comparison (optional) ===
    if args.legacy:
        print(f"\n{'='*70}")
        print(f"⚠️  Legacy 模式比較（單筆交易 bootstrap — 有方法論缺陷）")
        print(f"{'='*70}")
        trades = get_trades(args.trades)
        if trades:
            returns = [t['return'] for t in trades]
            print(f"   交易筆數: {len(returns)}")

            legacy_stats = legacy_simulate(
                returns, args.runs, block_size=5
            )
            print(f"   Legacy 最差 5% 報酬: {legacy_stats['p5_ret']*100:+.1f}%")
            print(f"   Legacy 最差 5% MDD:  {legacy_stats['p5_mdd']*100:.1f}%")
            print(f"   Legacy 中位數報酬:   {legacy_stats['median_ret']*100:+.1f}%")
            print()
            print(f"   ⚠️  Legacy 模式用固定 10% 倉位順序乘上去，")
            print(f"      丟掉了多檔同持、regime 縮放、資金占用等組合效應。")
            print(f"      結果通常比 Equity-Curve 模式更樂觀（虛假安全感）。")
        else:
            print("   ❌ 無交易數據")


if __name__ == '__main__':
    main()
