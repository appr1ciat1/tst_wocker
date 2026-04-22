#!/usr/bin/env python3
"""
FinLab 啟發因子 — 整合測試腳本

一鍵執行完整驗證流程：
1. 前視偏差驗證 (verify_strategy.py)
2. 因子消融分析 (factor_grid_search.py --mode ablation)
3. 結果分析與結論

使用方式：
    python test_finlab_factors.py              # 完整測試
    python test_finlab_factors.py --quick       # 快速測試（靜態股池）
    python test_finlab_factors.py --verify-only # 只跑前視偏差驗證
"""

import argparse
import subprocess
import sys
import os
import re
from datetime import datetime


def run_cmd(cmd, desc=""):
    """執行命令並回傳 stdout+stderr。"""
    print(f"\n{'='*60}")
    print(f"🔧 {desc}")
    print(f"   $ {cmd}")
    print(f"{'='*60}\n")

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    output = result.stdout + result.stderr

    # 印出但限制長度
    lines = output.split('\n')
    for line in lines[:200]:
        print(line)
    if len(lines) > 200:
        print(f"... (已截斷 {len(lines) - 200} 行)")

    return result.returncode, output


def extract_metrics(output):
    """從 ai_report.py 輸出中擷取關鍵指標。"""
    def get(pattern, default=0):
        m = re.search(pattern, output)
        return float(m.group(1)) if m else default

    return {
        'ann': get(r'年化報酬率:\s+([\+\-\d\.]+)%'),
        'sharpe': get(r'Sharpe Ratio:\s+([\+\-\d\.]+)'),
        'sortino': get(r'Sortino Ratio:\s+([\+\-\d\.]+)'),
        'mdd': get(r'最大回撤:\s+([\+\-\d\.]+)%'),
        'calmar': get(r'Calmar Ratio:\s+([\+\-\d\.]+)'),
        'trades': int(get(r'共 (\d+) 筆交易')),
        'win_rate': get(r'勝率\s*([\d\.]+)%'),
        'pf': get(r'Profit Factor:\s+([\d\.]+)'),
    }


def main():
    parser = argparse.ArgumentParser(description='FinLab 因子整合測試')
    parser.add_argument('--quick', action='store_true',
                        help='快速測試（靜態股池，較短回測期）')
    parser.add_argument('--verify-only', action='store_true',
                        help='只跑前視偏差驗證')
    parser.add_argument('--skip-ablation', action='store_true',
                        help='跳過完整 ablation（只跑關鍵組合）')
    args = parser.parse_args()

    print("🚀" + "=" * 58)
    print("   FinLab 啟發因子 — 整合測試流程")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = {}

    # === Step 1: 前視偏差驗證 ===
    code, output = run_cmd(
        'python3 verify_strategy.py --verbose',
        '步驟 1/3: 前視偏差驗證'
    )
    results['verify'] = code == 0

    if args.verify_only:
        print("\n✅ 前視偏差驗證完成（--verify-only 模式）")
        return

    # === Step 2: 快速因子測試（對齊 baseline）===
    pool_flag = '--static-pool' if args.quick else ''
    days_flag = '--days 800' if args.quick else '--days 1200'

    # 2a. Baseline
    code, output = run_cmd(
        f'python3 ai_report.py {pool_flag} {days_flag}',
        '步驟 2a: v8.5 Baseline 回測'
    )
    baseline = extract_metrics(output)
    results['baseline'] = baseline
    print(f"\n📊 Baseline: Sharpe={baseline['sharpe']:.3f} "
          f"Ann={baseline['ann']:+.1f}% MDD={baseline['mdd']:.1f}%")

    # 2b. Baseline + RSI
    code, output = run_cmd(
        f'python3 ai_report.py {pool_flag} {days_flag} --rsi-weight 1.0',
        '步驟 2b: Baseline + RSI-20 (weight=1.0)'
    )
    rsi_result = extract_metrics(output)
    results['baseline_rsi'] = rsi_result
    delta = rsi_result['sharpe'] - baseline['sharpe']
    marker = '🆕' if delta > 0.05 else ('🔴' if delta < -0.1 else '⚠️')
    print(f"\n📊 +RSI: Sharpe={rsi_result['sharpe']:.3f} "
          f"(Δ={delta:+.3f} {marker})")

    # 2c. Baseline + Breakout
    code, output = run_cmd(
        f'python3 ai_report.py {pool_flag} {days_flag} --breakout-weight 1.0',
        '步驟 2c: Baseline + Breakout-300 (weight=1.0)'
    )
    breakout_result = extract_metrics(output)
    results['baseline_breakout'] = breakout_result
    delta = breakout_result['sharpe'] - baseline['sharpe']
    marker = '🆕' if delta > 0.05 else ('🔴' if delta < -0.1 else '⚠️')
    print(f"\n📊 +Breakout: Sharpe={breakout_result['sharpe']:.3f} "
          f"(Δ={delta:+.3f} {marker})")

    # 2d. Baseline + RevMomentum
    code, output = run_cmd(
        f'python3 ai_report.py {pool_flag} {days_flag} --rev-momentum-weight 1.0',
        '步驟 2d: Baseline + RevMomentum-60 (weight=1.0)'
    )
    revmom_result = extract_metrics(output)
    results['baseline_revmom'] = revmom_result
    delta = revmom_result['sharpe'] - baseline['sharpe']
    marker = '🆕' if delta > 0.05 else ('🔴' if delta < -0.1 else '⚠️')
    print(f"\n📊 +RevMom: Sharpe={revmom_result['sharpe']:.3f} "
          f"(Δ={delta:+.3f} {marker})")

    # === Step 3: 完整 Factor Grid Search ===
    if not args.skip_ablation:
        code, output = run_cmd(
            f'python3 factor_grid_search.py --mode ablation {pool_flag} {days_flag}',
            '步驟 3: 完整因子組合搜索'
        )
        results['grid_search'] = code == 0

    # === Summary ===
    print("\n\n" + "=" * 60)
    print("📋 FinLab 因子整合測試 — 最終結果")
    print("=" * 60)

    print(f"\n{'Configuration':<35s} | {'Sharpe':>7s} | {'Ann%':>7s} | "
          f"{'MDD%':>7s} | {'ΔSharpe':>8s}")
    print("-" * 75)

    all_results = [
        ('v8.5 Baseline ⭐', baseline),
        ('+ RSI-20 (w=1.0)', rsi_result),
        ('+ Breakout-300 (w=1.0)', breakout_result),
        ('+ RevMomentum-60 (w=1.0)', revmom_result),
    ]

    for label, r in all_results:
        delta = r['sharpe'] - baseline['sharpe']
        marker = ''
        if delta > 0.05:
            marker = ' 🆕'
        elif delta < -0.1:
            marker = ' 🔴'
        print(f"{label:<35s} | {r['sharpe']:>7.3f} | {r['ann']:>+6.1f}% | "
              f"{r['mdd']:>6.1f}% | {delta:>+7.3f}{marker}")

    print("-" * 75)

    # 找最佳
    best_label, best = max(all_results, key=lambda x: x[1]['sharpe'])
    if best_label == 'v8.5 Baseline ⭐':
        print("\n✅ 結論：v8.5 Baseline 仍為最優，新 FinLab 因子無法超越")
        print("   策略維持不變，記錄消融結果供未來參考")
    else:
        improvement = (best['sharpe'] / baseline['sharpe'] - 1) * 100
        print(f"\n🆕 發現潛在改進：{best_label}")
        print(f"   Sharpe {best['sharpe']:.3f} vs baseline {baseline['sharpe']:.3f} "
              f"(+{improvement:.1f}%)")
        print(f"   ⚠️ 必須經 walk_forward.py + monte_carlo.py 驗證")

    print(f"\n{'='*60}")
    print(f"   測試完成於 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
