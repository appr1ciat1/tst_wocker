#!/usr/bin/env python3
import argparse
import itertools
import sys
sys.path.insert(0, '.')
from run_full_sweep import run_v9, enrich, load_bundle

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

bundle = load_bundle(A)
v85 = enrich(run_v9(bundle, A, 'v85', hybrid_tiered=False), 0, 0)
ann_bar, mdd_bar = v85['ann_return_pct'], v85['max_dd_pct']
base = dict(rotation_trigger_vol=0.22, crisis_vol=0.30, sat_alpha_trim_frac=0.0)
rows = []
for sf, cb, ct, cd, cc in itertools.product(
    [0.82, 0.85, 0.88, 0.92, 1.0],
    [1.95, 2.05, 2.10, 2.15, 2.25],
    [0.75, 0.80, 0.85, 0.90],
    [12, 14, 16, 18],
    [1.25, 1.35, 1.45],
):
    label = f'ft sf{int(sf*100)} cb{cb:.2f} ct{int(ct*100)} d{cd}'
    row = enrich(run_v9(bundle, A, label, **base,
        stress_sat_floor=sf, cooling_sat_boost=cb,
        core_alpha_trim_frac=ct, cooling_days=cd, stress_core_ceiling=cc), ann_bar, mdd_bar)
    rows.append(row)

rows.sort(key=lambda r: r['composite'], reverse=True)
print(f'v8.5 ann {ann_bar:+.1f}% MDD {mdd_bar:+.1f}%')
print(f'dual winners: {sum(1 for r in rows if r["dual_win"])} / {len(rows)}')
for r in rows[:15]:
    flag = '🏆' if r['dual_win'] else '  '
    print(f"{flag} {r['label']:<28} ann {r['ann_return_pct']:+6.1f}% MDD {r['max_dd_pct']:+6.1f}% "
          f"Sharpe {r['sharpe']:.3f} score {r['composite']:+.2f}")