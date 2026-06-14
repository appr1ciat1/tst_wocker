#!/usr/bin/env python3
"""快速對照 v8.5 vs v9（完整 backtester，含冷卻輪動）。"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_vol_rotation import load_bundle, run_case
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--capital', type=float, default=200_000)
parser.add_argument('--days', type=int, default=1200)
parser.add_argument('--rotation-trigger', type=float, default=0.22)
parser.add_argument('--crisis-vol', type=float, default=0.30)
args = parser.parse_args()

class A:
    days = args.days
    start_date = end_date = eval_start = None
    top_k = 7
    hold_days = 20
    tp_atr = 4.0
    sl_atr = 3.0
    gap_filter = 1.5
    regime_floor = 0.10
    capital = args.capital
    position_size = 0.10
    universe_size = 60

bundle = load_bundle(A)
v85 = run_case(bundle, A, False, label='v8.5')
v9 = run_case(
    bundle, A, True,
    rotation_trigger=args.rotation_trigger,
    crisis_vol=args.crisis_vol,
    label=f'v9 {args.rotation_trigger:.0%}/{args.crisis_vol:.0%} +cooling',
)

print('\n' + '=' * 60)
print('對照（壓力溫和縮Sat；冷卻 Coreα→Sat 2.1× 16日；crisis 30%）')
print('=' * 60)
for r in (v85, v9):
    print(
        f"{r['label']:<32} 年化 {r['ann_return_pct']:+6.1f}%  "
        f"MDD {r['max_dd_pct']:+6.1f}%  Sharpe {r['sharpe']:.3f}  "
        f"交易 {r['trades']}"
    )
beats_ann = v9['ann_return_pct'] > v85['ann_return_pct']
beats_mdd = v9['max_dd_pct'] > v85['max_dd_pct']
print(f"\n超越 v8.5 報酬: {'是' if beats_ann else '否'} ({v9['ann_return_pct']-v85['ann_return_pct']:+.1f}pp)")
print(f"優於 v8.5 回撤: {'是' if beats_mdd else '否'} ({v9['max_dd_pct']-v85['max_dd_pct']:+.1f}pp)")
print(f"雙贏: {'是' if beats_ann and beats_mdd else '否'}")