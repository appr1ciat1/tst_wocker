#!/usr/bin/env python3
"""分階段完整 v9 掃描：目標 ann > v8.5 且 MDD 優於 v8.5。"""

import argparse
import itertools
import json
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.event_backtest import EventDrivenBacktester
from strategy.evaluation import slice_evaluation_window
from strategy.risk_metrics import compute_risk_metrics
from sweep_vol_rotation import load_bundle

DEFAULTS = {
    'stress_sat_floor': 0.75,
    'stress_core_ceiling': 1.30,
    'cooling_sat_boost': 2.00,
    'cooling_core_boost': 0.50,
    'sat_alpha_trim_frac': 0.30,
    'sat_alpha_trim_min_pnl': 0.02,
    'core_alpha_trim_frac': 0.70,
    'core_alpha_trim_min_pnl': 0.005,
    'cooling_days': 12,
}


def run_v9(bundle, args, label, hybrid_tiered=True, **kw):
    close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60 = bundle
    params = {**DEFAULTS, **kw}
    bt = EventDrivenBacktester(
        tp_sl_mode='atr',
        tp_atr_mult=args.tp_atr,
        sl_atr_mult=args.sl_atr,
        max_hold_days=args.hold_days,
        initial_capital=args.capital,
        position_size=args.position_size,
        regime_filter=True,
        regime_graduated=True,
        regime_floor=args.regime_floor,
        gap_filter_atr=args.gap_filter,
        breadth_regime=True,
        hybrid_tiered=hybrid_tiered,
        core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15,
        buy_cost=0.001425,
        sell_cost=0.004425,
        rotation_trigger_vol=params.get('rotation_trigger_vol', 0.22),
        crisis_vol=params.get('crisis_vol', 0.35),
        stress_sat_floor=params['stress_sat_floor'],
        stress_core_ceiling=params['stress_core_ceiling'],
        cooling_sat_boost=params['cooling_sat_boost'],
        cooling_core_boost=params['cooling_core_boost'],
        sat_alpha_trim_frac=params['sat_alpha_trim_frac'],
        sat_alpha_trim_min_pnl=params['sat_alpha_trim_min_pnl'],
        core_alpha_trim_frac=params['core_alpha_trim_frac'],
        core_alpha_trim_min_pnl=params['core_alpha_trim_min_pnl'],
        cooling_days=params['cooling_days'],
    )
    trades_df, equity_df = bt.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=args.top_k, threshold=2.0,
        market_close=market_close, vol_df=vol_df, universe_mask=universe_mask,
    )
    report_eq, report_tr = slice_evaluation_window(
        equity_df, trades_df, eval_start=args.eval_start, initial_capital=args.capital,
    )
    m = compute_risk_metrics(report_eq, report_tr, args.capital)
    row = {
        'label': label,
        'hybrid_tiered': hybrid_tiered,
        'ann_return_pct': round(m['ann_return'] * 100, 2),
        'max_dd_pct': round(m['max_drawdown_pct'] * 100, 2),
        'sharpe': round(m['sharpe'], 3),
        'calmar': round(m['calmar'], 3),
        'trades': int(m['total_trades']),
        **params,
    }
    return row


def enrich(row, ann_bar, mdd_bar):
    ann_gap = row['ann_return_pct'] - ann_bar
    mdd_gap = row['max_dd_pct'] - mdd_bar
    row['ann_gap'] = round(ann_gap, 2)
    row['mdd_gap'] = round(mdd_gap, 2)
    row['dual_win'] = ann_gap > 0 and mdd_gap > 0
    row['composite'] = round(ann_gap + 0.6 * mdd_gap + 0.2 * (row['sharpe'] - 2.15), 3)
    return row


def print_row(row, prefix=''):
    print(
        f"{prefix}{row['label']:<30} ann {row['ann_return_pct']:+6.1f}%  "
        f"MDD {row['max_dd_pct']:+6.1f}%  Sharpe {row['sharpe']:.3f}  "
        f"score {row['composite']:+.2f}  {'🏆' if row['dual_win'] else ''}"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--capital', type=float, default=200_000)
    p.add_argument('--days', type=int, default=1200)
    p.add_argument('--top-k', type=int, default=7)
    p.add_argument('--hold-days', type=int, default=20)
    p.add_argument('--tp-atr', type=float, default=4.0)
    p.add_argument('--sl-atr', type=float, default=3.0)
    p.add_argument('--gap-filter', type=float, default=1.5)
    p.add_argument('--regime-floor', type=float, default=0.10)
    p.add_argument('--position-size', type=float, default=0.10)
    p.add_argument('--universe-size', type=int, default=60)
    p.add_argument('--eval-start', default=None)
    p.add_argument('--start-date', default=None)
    p.add_argument('--end-date', default=None)
    args = p.parse_args()

    bundle = load_bundle(args)
    all_rows = []

    print('\n▶ v8.5 baseline')
    v85 = enrich(run_v9(bundle, args, 'v8.5', hybrid_tiered=False), 0, 0)
    ann_bar, mdd_bar = v85['ann_return_pct'], v85['max_dd_pct']
    v85 = enrich(v85, ann_bar, mdd_bar)
    print_row(v85)
    all_rows.append(v85)

    # Stage 1: 門檻 × 壓力 Sat 下限
    print('\n▶ Stage 1: trigger × crisis × stress_sat_floor')
    s1 = []
    for trig, cri, sf in itertools.product(
        [0.15, 0.18, 0.22], [0.28, 0.30, 0.35], [0.65, 0.75, 0.85],
    ):
        if cri <= trig:
            continue
        label = f's1 t{int(trig*100)} c{int(cri*100)} sf{int(sf*100)}'
        row = enrich(run_v9(bundle, args, label,
                            rotation_trigger_vol=trig, crisis_vol=cri, stress_sat_floor=sf), ann_bar, mdd_bar)
        s1.append(row)
        if row['dual_win']:
            print_row(row, '🏆 ')
    s1.sort(key=lambda r: r['composite'], reverse=True)
    best1 = s1[0]
    print_row(best1, '★ ')
    all_rows.extend(s1)

    base = {k: best1[k] for k in ('rotation_trigger_vol', 'crisis_vol', 'stress_sat_floor')}

    # Stage 2: 冷卻加碼 × Sat 主動減碼
    print('\n▶ Stage 2: cooling_sat_boost × sat_alpha_trim_frac')
    s2 = []
    for cb, st in itertools.product([1.70, 1.90, 2.10, 2.30], [0.0, 0.25, 0.40]):
        label = f's2 cb{cb:.1f} st{int(st*100)}'
        row = enrich(run_v9(bundle, args, label, **base, cooling_sat_boost=cb, sat_alpha_trim_frac=st),
                     ann_bar, mdd_bar)
        s2.append(row)
        if row['dual_win']:
            print_row(row, '🏆 ')
    s2.sort(key=lambda r: r['composite'], reverse=True)
    best2 = s2[0]
    print_row(best2, '★ ')
    all_rows.extend(s2)
    base.update({k: best2[k] for k in ('cooling_sat_boost', 'sat_alpha_trim_frac')})

    # Stage 3: Core 了結比例 × 壓力 Core 上限 × 冷卻天數
    print('\n▶ Stage 3: core_trim × core_ceiling × cooling_days')
    s3 = []
    for ct, cc, cd in itertools.product([0.55, 0.70, 0.85], [1.15, 1.30, 1.50], [8, 12, 16]):
        label = f's3 ct{int(ct*100)} cc{cc:.2f} d{cd}'
        row = enrich(run_v9(bundle, args, label, **base,
                            core_alpha_trim_frac=ct, stress_core_ceiling=cc, cooling_days=cd),
                     ann_bar, mdd_bar)
        s3.append(row)
        if row['dual_win']:
            print_row(row, '🏆 ')
    s3.sort(key=lambda r: r['composite'], reverse=True)
    best3 = s3[0]
    print_row(best3, '★ ')
    all_rows.extend(s3)

    # Stage 4: 邏輯變體（驗證核心假設）
    print('\n▶ Stage 4: 邏輯變體')
    variants = [
        ('V0 現行預設(0.40/2.0)', {
            'rotation_trigger_vol': 0.22, 'crisis_vol': 0.35,
            'stress_sat_floor': 0.40, 'stress_core_ceiling': 2.0,
            'cooling_sat_boost': 1.90, 'sat_alpha_trim_frac': 0.50,
            'core_alpha_trim_frac': 0.70, 'cooling_days': 12,
        }),
        ('V1 僅sizing輪動(不主動賣Sat)', {
            **{k: best3[k] for k in (
                'rotation_trigger_vol', 'crisis_vol', 'stress_sat_floor', 'stress_core_ceiling',
                'cooling_sat_boost', 'core_alpha_trim_frac', 'cooling_days',
            )},
            'sat_alpha_trim_frac': 0.0,
        }),
        ('V2 僅冷卻主動輪動(壓力不縮Sat)', {
            **{k: best3[k] for k in (
                'rotation_trigger_vol', 'crisis_vol', 'cooling_sat_boost',
                'core_alpha_trim_frac', 'cooling_days',
            )},
            'stress_sat_floor': 1.0, 'stress_core_ceiling': 1.0,
            'sat_alpha_trim_frac': 0.0,
        }),
        ('V3 最佳參數組', {k: best3[k] for k in (
            'rotation_trigger_vol', 'crisis_vol', 'stress_sat_floor', 'stress_core_ceiling',
            'cooling_sat_boost', 'sat_alpha_trim_frac', 'core_alpha_trim_frac', 'cooling_days',
        )}),
        ('V4 最佳+更積極冷卻', {
            **{k: best3[k] for k in (
                'rotation_trigger_vol', 'crisis_vol', 'stress_sat_floor', 'stress_core_ceiling',
                'sat_alpha_trim_frac', 'core_alpha_trim_frac', 'cooling_days',
            )},
            'cooling_sat_boost': max(2.2, best3['cooling_sat_boost']),
            'core_alpha_trim_frac': min(0.85, best3['core_alpha_trim_frac'] + 0.10),
        }),
    ]
    s4 = []
    for label, kw in variants:
        row = enrich(run_v9(bundle, args, label, **kw), ann_bar, mdd_bar)
        s4.append(row)
        print_row(row, '🏆 ' if row['dual_win'] else '  ')
    all_rows.extend(s4)

    df = pd.DataFrame(all_rows)
    v9_df = df[df['hybrid_tiered'] == True].copy()
    winners = v9_df[v9_df['dual_win']].sort_values('composite', ascending=False)

    os.makedirs('artifacts', exist_ok=True)
    df.to_csv('artifacts/full_sweep_v9.csv', index=False)
    summary = {
        'created_at': datetime.now().isoformat(),
        'v85_baseline': v85,
        'stage_best': {'s1': best1, 's2': best2, 's3': best3},
        'variants': s4,
        'dual_win_count': int(len(winners)),
        'winners': winners.head(20).to_dict('records'),
        'best_overall': winners.iloc[0].to_dict() if len(winners) else v9_df.sort_values('composite', ascending=False).iloc[0].to_dict(),
    }
    with open('artifacts/full_sweep_v9.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    print('\n' + '=' * 72)
    print(f"v8.5: ann {ann_bar:+.1f}% | MDD {mdd_bar:+.1f}% | Sharpe {v85['sharpe']:.3f}")
    print(f"雙贏: {len(winners)} / {len(v9_df)} v9 組合")
    best = summary['best_overall']
    print(
        f"最佳: {best['label']} → ann {best['ann_return_pct']:+.1f}% | "
        f"MDD {best['max_dd_pct']:+.1f}% | Sharpe {best['sharpe']:.3f} | "
        f"dual={'是' if best.get('dual_win') else '否'}"
    )
    print('📁 artifacts/full_sweep_v9.csv / .json')


if __name__ == '__main__':
    main()