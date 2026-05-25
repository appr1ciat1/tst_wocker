#!/usr/bin/env python3
"""
季度自動重新校準腳本 (Quarterly Auto-Recalibration)

每季度掃描核心參數，確認當前配置仍為最優。
若發現更優配置，輸出建議（但不自動修改）。

使用方式:
  python sweep.py                # 完整掃描
  python sweep.py --quick        # 快速掃描（核心參數）
  python sweep.py --output log   # 輸出結果到 sweep_log.csv
"""

import subprocess
import re
import sys
import csv
from datetime import datetime
from itertools import product


def run_backtest(args_str):
    """Run ai_report.py with given args and extract metrics."""
    cmd = f'{sys.executable} ai_report.py {args_str}'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise RuntimeError(
            f"Backtest command failed with exit code {r.returncode}: {cmd}\n"
            f"{out[-2000:]}"
        )

    def get(pattern, label):
        m = re.search(pattern, out)
        if not m:
            raise ValueError(f"Failed to parse {label} from backtest output")
        return float(m.group(1))

    def get_first(patterns, label):
        for pattern in patterns:
            m = re.search(pattern, out)
            if m:
                return float(m.group(1))
        raise ValueError(f"Failed to parse {label} from backtest output")

    return {
        'ann': get(r'年化報酬率:\s+([\+\-\d\.]+)%', 'annual return'),
        'sharpe': get(r'Sharpe Ratio:\s+([\+\-\d\.]+)', 'Sharpe'),
        'sortino': get(r'Sortino Ratio:\s+([\+\-\d\.]+)', 'Sortino'),
        'calmar': get(r'Calmar Ratio:\s+([\+\-\d\.]+)', 'Calmar'),
        'mdd': get(r'最大回撤:\s+([\+\-\d\.]+)%', 'max drawdown'),
        'trades': int(get_first([r'總交易數:\s+(\d+)', r'共 (\d+) 筆交易'], 'trade count')),
        'win_rate': get_first([r'勝率[:：]\s*([\d\.]+)%', r'勝率\s*([\d\.]+)%'], 'win rate'),
        'pf': get(r'Profit Factor:\s+(inf|[\d\.]+)', 'profit factor'),
    }


def full_sweep():
    """Full parameter sweep: ~40 configs."""
    configs = []

    # Core parameters to sweep
    tp_atrs = [3.5, 4.0, 4.5]
    sl_atrs = [2.5, 3.0, 3.5]
    hold_days = [15, 20, 25, 30]
    top_ks = [4, 5, 6]
    gap_filters = [1.0, 1.5, 2.0]

    # Only sweep 1-2 params at a time (avoid combinatorial explosion)
    # Phase 1: hold_days × top_k
    for hd, k in product(hold_days, top_ks):
        configs.append((
            f'hold={hd}+k={k}',
            f'--hold-days {hd} --top-k {k}'
        ))

    # Phase 2: tp × sl (with best hold/k)
    for tp, sl in product(tp_atrs, sl_atrs):
        configs.append((
            f'tp={tp}+sl={sl}',
            f'--tp-atr {tp} --sl-atr {sl}'
        ))

    # Phase 3: gap filter
    for gf in gap_filters:
        configs.append((f'gap={gf}', f'--gap-filter {gf}'))

    # Phase 4: structural features
    configs.extend([
        ('mean-rev', '--mean-reversion'),
        ('dyn-risk', '--dynamic-risk'),
        ('fut-hedge', '--futures-hedge'),
        ('all-struct', '--mean-reversion --dynamic-risk --futures-hedge'),
    ])

    # Phase 5: Sector Flow Tilt
    configs.extend([
        ('sft', '--sector-flow-tilt'),
        ('sft+str0.5', '--sector-flow-tilt --tilt-strength 0.5'),
        ('sft+str0.7', '--sector-flow-tilt --tilt-strength 0.7'),
        ('sft+w5,10,15', '--sector-flow-tilt --tilt-windows 5,10,15'),
        ('sft+str0.7+w5,10,15', '--sector-flow-tilt --tilt-strength 0.7 --tilt-windows 5,10,15'),
    ])

    return configs


def quick_sweep():
    """Quick parameter sweep: ~17 configs."""
    return [
        ('baseline', ''),
        ('hold=15', '--hold-days 15'),
        ('hold=20', '--hold-days 20'),
        ('hold=25', '--hold-days 25'),
        ('hold=30', '--hold-days 30'),
        ('k=4', '--top-k 4'),
        ('k=5', '--top-k 5'),
        ('k=6', '--top-k 6'),
        ('tp=3.5', '--tp-atr 3.5'),
        ('tp=4.5', '--tp-atr 4.5'),
        ('sl=2.5', '--sl-atr 2.5'),
        ('sl=3.5', '--sl-atr 3.5'),
        ('dyn-risk', '--dynamic-risk'),
        ('gap=1.5+dyn', '--gap-filter 1.5 --dynamic-risk'),
        ('slip=0.1%', '--slippage 0.001'),
        ('slip=0.2%', '--slippage 0.002'),
    ]


def main():
    import argparse
    parser = argparse.ArgumentParser(description='季度參數重新校準')
    parser.add_argument('--quick', action='store_true', help='快速掃描')
    parser.add_argument('--output', choices=['log', 'csv'], help='輸出格式')
    args = parser.parse_args()

    configs = quick_sweep() if args.quick else full_sweep()

    print(f"🔄 季度重新校準 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"   掃描 {len(configs)} 組配置...")
    print()

    results = []
    total = len(configs)
    for idx, (name, cmd_args) in enumerate(configs):
        sys.stderr.write(f'[{idx+1}/{total}] {name}...\n')
        sys.stderr.flush()
        try:
            metrics = run_backtest(cmd_args)
            metrics['name'] = name
            metrics['args'] = cmd_args
            results.append(metrics)
        except Exception as e:
            print(f'   ⚠️ {name} failed: {e}')

    # Sort by Sharpe
    results.sort(key=lambda x: x['sharpe'], reverse=True)

    # Print results
    header = f"{'Config':<24s} | {'Ann':>7s} | {'Sharpe':>6s} | {'Sort':>5s} | {'Calmar':>6s} | {'MDD':>7s} | {'#Tr':>4s} | {'WR':>5s} | {'PF':>4s}"
    sep = '-' * len(header)
    print(header)
    print(sep)

    baseline_sharpe = None
    for r in results:
        if r['name'] == 'baseline' or (baseline_sharpe is None and r['args'] == ''):
            baseline_sharpe = r['sharpe']
            marker = ' ⭐'
        elif r['sharpe'] > (baseline_sharpe or 0):
            marker = ' 🆕'
        else:
            marker = ''
        print(f"{r['name']:<24s} | {r['ann']:>+6.1f}% | {r['sharpe']:>6.3f} | {r['sortino']:>5.2f} | "
              f"{r['calmar']:>6.3f} | {r['mdd']:>6.1f}% | {r['trades']:>4d} | {r['win_rate']:>4.1f}% | {r['pf']:>4.2f}{marker}")

    # Summary + degradation alert
    print(sep)
    best = results[0]
    exit_code = 0
    alerts = []

    # v8.1 honest baseline: Sharpe ~1.95, MDD ~-30%
    # Alert thresholds set at ~60% of honest baseline
    if baseline_sharpe and baseline_sharpe < 1.2:
        alerts.append(f"🚨 Baseline Sharpe {baseline_sharpe:.3f} < 1.2！策略可能已劣化")
        exit_code = 1

    # MDD 檢查
    baseline_result = next((r for r in results if r['name'] == 'baseline' or r['args'] == ''), None)
    if baseline_result and baseline_result['mdd'] < -35:
        alerts.append(f"🚨 Baseline MDD {baseline_result['mdd']:.1f}% < -35%！風險偏高")
        exit_code = 1

    if alerts:
        for a in alerts:
            print(f"\n{a}")
        print(f"   建議立即手動檢查市場環境與參數適配性")
        print(f"   暫停新倉直到問題排除")
        # 嘗試發 Telegram 警報
        try:
            import os
            import urllib.request
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
            chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
            if bot_token and chat_id:
                msg = "⚠️ Sweep 警報\n" + "\n".join(alerts)
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                data = urllib.parse.urlencode({'chat_id': chat_id, 'text': msg}).encode()
                urllib.request.urlopen(url, data, timeout=10)
                print("   📱 已發送 Telegram 警報")
        except Exception:
            pass
    elif baseline_sharpe and best['sharpe'] > baseline_sharpe * 1.05:
        print(f"\n⚠️  發現更優配置: {best['name']} (Sharpe {best['sharpe']:.3f} vs baseline {baseline_sharpe:.3f})")
        print(f"   建議命令: python ai_report.py {best['args']}")
    else:
        print(f"\n✅ 當前配置仍為最優或差異 < 5%")

    # CSV output
    if args.output in ('log', 'csv'):
        filename = f"sweep_log_{datetime.now().strftime('%Y%m%d')}.csv"
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['name', 'args', 'ann', 'sharpe', 'sortino',
                                                    'calmar', 'mdd', 'trades', 'win_rate', 'pf'])
            writer.writeheader()
            writer.writerows(results)
        print(f"📄 結果已儲存至 {filename}")

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
