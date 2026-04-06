#!/usr/bin/env python3
"""
Monte Carlo 壓力測試

對歷史交易做 bootstrap 重採樣，估算策略在極端情境下的表現分布。
用來量化「最壞情況」，確保策略不會在不利 regime 中崩潰。

使用方式:
  python monte_carlo.py                # 預設 2000 次模擬
  python monte_carlo.py --runs 5000    # 更精確
  python monte_carlo.py --confidence 99  # 99% 信心區間
"""

import subprocess
import re
import sys
import argparse
import random
import statistics
from datetime import datetime


def get_trades():
    """從最新回測取得交易列表。"""
    cmd = 'python ai_report.py'
    print("📥 執行回測以取得交易數據...")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr

    # 解析交易的 Return_Pct
    # 從 artifacts/trades CSV 讀取
    import csv
    import os
    import glob

    csv_files = glob.glob('artifacts/trades_*.csv')
    if not csv_files:
        # 嘗試直接從 stdout 解析
        print("⚠️ 無法找到 trades CSV，嘗試從輸出解析...")
        returns = re.findall(r'Return_Pct[^\d]*([\-\d\.]+)', out)
        if returns:
            return [float(r) for r in returns]
        else:
            print("❌ 無法取得交易數據")
            sys.exit(1)

    latest = max(csv_files, key=os.path.getmtime)
    print(f"   讀取 {latest}...")
    returns = []
    with open(latest) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                returns.append(float(row['Return_Pct']))
            except (KeyError, ValueError):
                continue

    return returns


def simulate_equity(returns, initial=1_000_000, position_size=0.10):
    """用隨機重採樣的交易序列模擬權益曲線。"""
    equity = initial
    peak = initial
    max_dd = 0

    for ret in returns:
        trade_pnl = equity * position_size * ret
        equity += trade_pnl
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)

    total_return = (equity / initial - 1)
    return total_return, max_dd, equity


def main():
    parser = argparse.ArgumentParser(description='Monte Carlo 壓力測試')
    parser.add_argument('--runs', type=int, default=2000, help='模擬次數 (預設 2000)')
    parser.add_argument('--confidence', type=int, default=95, help='信心區間 (預設 95)')
    args = parser.parse_args()

    trades = get_trades()
    n_trades = len(trades)

    if n_trades < 30:
        print(f"⚠️ 交易筆數 {n_trades} 太少，Monte Carlo 結果不可靠")
        sys.exit(1)

    print(f"\n📊 Monte Carlo 壓力測試 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"   交易筆數: {n_trades}")
    print(f"   模擬次數: {args.runs}")
    print(f"   信心區間: {args.confidence}%")

    # 原始序列績效
    orig_ret, orig_mdd, _ = simulate_equity(trades)
    print(f"\n📈 原始序列:")
    print(f"   總報酬: {orig_ret*100:+.1f}%")
    print(f"   MDD:    {orig_mdd*100:.1f}%")

    # Monte Carlo 模擬
    all_returns = []
    all_mdds = []
    all_sharpes = []

    print(f"\n🎲 模擬中...")
    for run in range(args.runs):
        # Bootstrap：有放回隨機重採樣
        sample = random.choices(trades, k=n_trades)
        ret, mdd, _ = simulate_equity(sample)
        all_returns.append(ret)
        all_mdds.append(mdd)

        # 簡化 Sharpe（用交易報酬）
        if len(sample) > 1:
            avg = statistics.mean(sample)
            std = statistics.stdev(sample)
            sh = (avg / std * (252**0.5)) if std > 0 else 0
            all_sharpes.append(sh)

        if (run + 1) % 500 == 0:
            sys.stderr.write(f'   [{run+1}/{args.runs}]\n')
            sys.stderr.flush()

    # 排序取百分位
    all_returns.sort()
    all_mdds.sort()
    all_sharpes.sort()

    tail = (100 - args.confidence) / 100
    tail_idx = int(len(all_returns) * tail)
    top_idx = int(len(all_returns) * (1 - tail))

    print(f"\n{'指標':<16s} | {'最差 {0}%'.format(100-args.confidence):>10s} | {'中位數':>10s} | {'最佳 {0}%'.format(100-args.confidence):>10s}")
    print("-" * 56)
    print(f"{'總報酬':<16s} | {all_returns[tail_idx]*100:>+9.1f}% | {statistics.median(all_returns)*100:>+9.1f}% | {all_returns[top_idx]*100:>+9.1f}%")
    print(f"{'MDD':<16s} | {all_mdds[tail_idx]*100:>9.1f}% | {statistics.median(all_mdds)*100:>9.1f}% | {all_mdds[top_idx]*100:>9.1f}%")
    if all_sharpes:
        print(f"{'Sharpe':<16s} | {all_sharpes[tail_idx]:>10.2f} | {statistics.median(all_sharpes):>10.2f} | {all_sharpes[top_idx]:>10.2f}")

    # 風險評估
    print(f"\n📊 風險評估:")
    worst_mdd = all_mdds[tail_idx]
    median_mdd = statistics.median(all_mdds)

    if abs(worst_mdd) < 0.22:
        print(f"   ✅ 最差 {100-args.confidence}% MDD = {worst_mdd*100:.1f}% < -22%，風險可控")
    elif abs(worst_mdd) < 0.30:
        print(f"   ⚠️ 最差 {100-args.confidence}% MDD = {worst_mdd*100:.1f}%，中等風險")
    else:
        print(f"   🚨 最差 {100-args.confidence}% MDD = {worst_mdd*100:.1f}%，風險偏高")

    worst_ret = all_returns[tail_idx]
    if worst_ret > 0:
        print(f"   ✅ 最差 {100-args.confidence}% 報酬仍為正 ({worst_ret*100:+.1f}%)")
    else:
        print(f"   ⚠️ 最差 {100-args.confidence}% 報酬為負 ({worst_ret*100:+.1f}%)")

    # 實盤建議
    print(f"\n💡 實盤建議:")
    print(f"   預期 MDD 區間: {median_mdd*100:.1f}% ~ {worst_mdd*100:.1f}%")
    suggested_capital = 100000 / abs(worst_mdd) if worst_mdd != 0 else 100000
    print(f"   建議起始資金: ≥ {suggested_capital:,.0f} 元（確保回撤不超過初始投入 10%）")


if __name__ == '__main__':
    main()
