#!/usr/bin/env python3
"""對照：冷卻期全體 Sat 2.1× vs 僅 top-3 加碼。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_full_sweep import load_bundle, run_v9, enrich

class A:
    capital = 200_000
    days = 1200
    top_k = 7
    hold_days = 20
    tp_atr = 4.0
    sl_atr = 3.0
    gap_filter = 1.5
    regime_floor = 0.10
    position_size = 0.10
    universe_size = 60
    eval_start = start_date = end_date = None

# Monkey-patch top-K for "all sat boost" baseline run
import strategy.portfolio_vol_target as pvt

bundle = load_bundle(A)
v85 = enrich(run_v9(bundle, A, 'v8.5', hybrid_tiered=False), 0, 0)
ann_bar, mdd_bar = v85['ann_return_pct'], v85['max_dd_pct']
v85 = enrich(v85, ann_bar, mdd_bar)

base_kw = dict(
    rotation_trigger_vol=0.22,
    crisis_vol=0.30,
    stress_sat_floor=0.85,
    stress_core_ceiling=1.50,
    sat_alpha_trim_frac=0.0,
    core_alpha_trim_frac=0.85,
    cooling_sat_boost=2.10,
    cooling_days=16,
)

orig_top_k = pvt.COOLING_SAT_TOP_K_DEFAULT
pvt.COOLING_SAT_TOP_K_DEFAULT = 99
all_boost = enrich(run_v9(bundle, A, 'v9 冷卻全體Sat 2.1×', **base_kw), ann_bar, mdd_bar)
pvt.COOLING_SAT_TOP_K_DEFAULT = orig_top_k
top3 = enrich(run_v9(bundle, A, 'v9 冷卻top3 Sat 2.1×', **base_kw), ann_bar, mdd_bar)

print('\n' + '=' * 68)
for r in (v85, all_boost, top3):
    dual = '🏆' if r.get('dual_win') else '  '
    print(
        f"{dual} {r['label']:<26} ann {r['ann_return_pct']:+6.1f}%  "
        f"MDD {r['max_dd_pct']:+6.1f}%  Sharpe {r['sharpe']:.3f}"
    )
print('=' * 68)
print(
    f"top3 vs 全體: ann {top3['ann_return_pct']-all_boost['ann_return_pct']:+.1f}pp  "
    f"MDD {top3['max_dd_pct']-all_boost['max_dd_pct']:+.1f}pp"
)