#!/usr/bin/env python3
"""
Anchored Walk-Forward OOS 驗證 v2

真正的 Out-of-Sample 測試：將歷史切成不重疊的時間段，
每段用固定參數回測，檢驗策略在不同市場環境下的穩定性。

與 v1 的差異：
- v1 只是改 --days 長度重跑同一策略，不是真正的 OOS
- v2 用 --start-date / --end-date 精確切割時間段
- v2 每段時間是完全不重疊的 out-of-sample 區間
- v2 加入了歷史市場環境標注（多頭/空頭/震盪）

使用方式:
  python walk_forward.py                # 預設 4 段 anchored
  python walk_forward.py --folds 6      # 6 段更細粒度
  python walk_forward.py --full-history # 用 2019 至今的完整歷史
"""

import subprocess
import re
import sys
import argparse
import statistics
from datetime import datetime, timedelta

import pandas as pd


def run_backtest_range(start_date, end_date, eval_start=None, extra_args=''):
    """Run ai_report.py with explicit date range and extract metrics."""
    cmd = (f'{sys.executable} ai_report.py '
           f'--start-date {start_date} --end-date {end_date} '
           f'{extra_args}')
    if eval_start:
        cmd += f' --eval-start {eval_start}'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
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


def build_anchored_windows(total_start, total_end, n_folds):
    """
    Build non-overlapping windows from total_start to total_end.

    Each window covers ~equal calendar days.
    """
    start = pd.Timestamp(total_start)
    end = pd.Timestamp(total_end)
    total_days = (end - start).days
    window_days = total_days // n_folds

    windows = []
    for i in range(n_folds):
        w_start = start + timedelta(days=i * window_days)
        w_end = start + timedelta(days=(i + 1) * window_days)
        if i == n_folds - 1:
            w_end = end  # last fold goes to exact end

        # Extend data fetch start by 120 days for warmup (MA60 + 60 buffer)
        fetch_start = w_start - timedelta(days=120)

        windows.append({
            'fold': i + 1,
            'fetch_start': fetch_start.strftime('%Y-%m-%d'),
            'eval_start': w_start.strftime('%Y-%m-%d'),
            'eval_end': w_end.strftime('%Y-%m-%d'),
        })

    return windows


def annotate_regime(start, end):
    """Annotate what market regime this period covers."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)

    annotations = []
    # COVID crash: 2020-02 ~ 2020-04
    if s <= pd.Timestamp('2020-04-30') and e >= pd.Timestamp('2020-02-01'):
        annotations.append('🦠 COVID崩盤')
    # COVID recovery: 2020-05 ~ 2021-06
    if s <= pd.Timestamp('2021-06-30') and e >= pd.Timestamp('2020-05-01'):
        annotations.append('📈 疫後復甦')
    # Rate hike: 2022-01 ~ 2022-10
    if s <= pd.Timestamp('2022-10-31') and e >= pd.Timestamp('2022-01-01'):
        annotations.append('📉 升息衝擊')
    # AI rally: 2023-01 ~ 2024-06
    if s <= pd.Timestamp('2024-06-30') and e >= pd.Timestamp('2023-01-01'):
        annotations.append('🤖 AI行情')
    # Recent
    if e >= pd.Timestamp('2024-07-01'):
        annotations.append('📊 近期')

    return ' '.join(annotations) if annotations else '—'


def main():
    parser = argparse.ArgumentParser(
        description='Anchored Walk-Forward OOS 驗證 v2'
    )
    parser.add_argument('--folds', type=int, default=4,
                        help='OOS 段數 (預設 4)')
    parser.add_argument('--full-history', action='store_true',
                        help='使用 2019 至今的完整歷史 (約 7 年)')
    parser.add_argument('--start', type=str, default=None,
                        help='自訂起始日期 (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None,
                        help='自訂結束日期 (YYYY-MM-DD)')
    args = parser.parse_args()

    if args.full_history:
        total_start = '2019-01-01'
    elif args.start:
        total_start = args.start
    else:
        # Default: ~4 years back
        total_start = (datetime.today() - timedelta(days=1500)).strftime('%Y-%m-%d')

    total_end = args.end or datetime.today().strftime('%Y-%m-%d')

    windows = build_anchored_windows(total_start, total_end, args.folds)

    print(f"📊 Anchored Walk-Forward OOS 驗證 v2 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"   總區間: {total_start} → {total_end}")
    print(f"   段數: {args.folds} (非重疊 OOS)")
    print(f"   每段含 120 天暖機期 (MA60 + buffer)，績效只統計 OOS eval window")
    print()
    print("   ⚠️  注意：這是固定參數的 OOS 穩定性測試，")
    print("        不是 nested walk-forward (train→test with param selection)。")
    print("        策略使用 v8.5 的固定參數，驗證其在不同時間段的表現。")
    print()

    header = (f"{'Fold':<6s} | {'期間':<24s} | {'市場環境':<16s} | "
              f"{'Ann':>7s} | {'Sharpe':>6s} | {'Calmar':>6s} | "
              f"{'MDD':>7s} | {'#Tr':>4s} | {'WR':>5s} | {'PF':>4s}")
    sep = '-' * len(header)
    print(header)
    print(sep)

    sharpes = []
    annuals = []
    mdds = []

    for w in windows:
        fold = w['fold']
        period = f"{w['eval_start']} → {w['eval_end']}"
        regime = annotate_regime(w['eval_start'], w['eval_end'])

        sys.stderr.write(f"[{fold}/{len(windows)}] {period}...\n")
        sys.stderr.flush()

        try:
            # Use fetch_start for warmup data, but report metrics only over
            # eval_start → eval_end.
            metrics = run_backtest_range(
                w['fetch_start'], w['eval_end'], eval_start=w['eval_start']
            )

            sharpes.append(metrics['sharpe'])
            annuals.append(metrics['ann'])
            mdds.append(metrics['mdd'])

            sh_color = '✅' if metrics['sharpe'] >= 1.5 else ('⚠️' if metrics['sharpe'] >= 0.5 else '🔴')

            print(f"  {fold:<4d} | {period:<24s} | {regime:<16s} | "
                  f"{metrics['ann']:>+6.1f}% | {metrics['sharpe']:>6.3f} {sh_color} | "
                  f"{metrics['calmar']:>6.3f} | {metrics['mdd']:>6.1f}% | "
                  f"{metrics['trades']:>4d} | {metrics['win_rate']:>4.1f}% | "
                  f"{metrics['pf']:>4.2f}")
        except Exception as e:
            print(f"  {fold:<4d} | {period:<24s} | {regime:<16s} | ❌ FAILED: {e}")

    print(sep)

    # Also run full period as reference
    print(f"\n📈 Full Period ({total_start} → {total_end})...")
    try:
        full_metrics = run_backtest_range(total_start, total_end)
        print(f"   Ann: {full_metrics['ann']:+.1f}%  Sharpe: {full_metrics['sharpe']:.3f}  "
              f"MDD: {full_metrics['mdd']:.1f}%  Trades: {full_metrics['trades']}")
    except Exception as e:
        print(f"   ❌ FAILED: {e}")
        full_metrics = None

    # Stability statistics
    if sharpes:
        avg_sh = statistics.mean(sharpes)
        min_sh = min(sharpes)
        max_sh = max(sharpes)
        std_sh = statistics.stdev(sharpes) if len(sharpes) > 1 else 0
        avg_mdd = statistics.mean(mdds)
        worst_mdd = min(mdds)

        print(f"\n{'='*60}")
        print(f"📊 OOS 穩定性統計")
        print(f"{'='*60}")
        print(f"   平均 Sharpe:     {avg_sh:.3f}")
        print(f"   最低 Sharpe:     {min_sh:.3f}")
        print(f"   最高 Sharpe:     {max_sh:.3f}")
        print(f"   Sharpe 標準差:   {std_sh:.3f}")
        print(f"   平均 MDD:        {avg_mdd:.1f}%")
        print(f"   最差 MDD:        {worst_mdd:.1f}%")

        if full_metrics:
            full_sh = full_metrics['sharpe']
            decay = avg_sh / full_sh if full_sh > 0 else 0
            print(f"\n   Full-period Sharpe: {full_sh:.3f}")
            print(f"   OOS 衰減比:         {decay:.2f} "
                  f"({'✅ >0.7 無過擬合跡象' if decay > 0.7 else '⚠️ <0.7 可能存在過擬合'})")

        # Per-fold consistency
        positive_folds = sum(1 for s in sharpes if s > 0)
        good_folds = sum(1 for s in sharpes if s >= 1.0)
        print(f"\n   正 Sharpe 段數:   {positive_folds}/{len(sharpes)}")
        print(f"   Sharpe ≥ 1.0:     {good_folds}/{len(sharpes)}")

        if min_sh >= 1.5:
            print(f"\n✅ 所有 OOS 段 Sharpe ≥ 1.5，策略穩定性極佳")
        elif min_sh >= 1.0:
            print(f"\n⚠️  部分 OOS 段 Sharpe < 1.5，需持續觀察")
        elif min_sh >= 0:
            print(f"\n⚠️  有 OOS 段 Sharpe < 1.0，策略可能在某些環境下表現平庸")
        else:
            print(f"\n🚨 有 OOS 段 Sharpe < 0，策略在某些環境下虧損！")


if __name__ == '__main__':
    main()
