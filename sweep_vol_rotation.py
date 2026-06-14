#!/usr/bin/env python3
"""掃描 rotation_trigger × crisis_vol，對照 v8.5 尋找最優 v9 參數。"""

import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_report import EXTENDED_TICKERS
from strategy.ai_strategy import fetch_panel_data, engineer_features, build_liquid_universe
from strategy.benchmark import fetch_benchmark
from strategy.event_backtest import EventDrivenBacktester
from strategy.evaluation import slice_evaluation_window
from strategy.risk_metrics import compute_risk_metrics
from strategy.portfolio_vol_target import ROTATION_TRIGGER_VOL, CRISIS_VOL

TRIGGER_GRID = [0.15, 0.18, 0.22]
CRISIS_GRID = [0.28, 0.30, 0.35]


def load_bundle(args):
    print("📥 下載資料並建構 Universe（共用）...")
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        EXTENDED_TICKERS, days=args.days,
        start_date=args.start_date, end_date=args.end_date,
    )
    universe_mask = build_liquid_universe(close_df, vol_df, top_n=args.universe_size)
    bench_raw = fetch_benchmark(
        '0050', days=args.days,
        start_date=args.start_date, end_date=args.end_date,
    )
    market_close = bench_raw * bench_raw.iloc[0] if len(bench_raw) > 0 else None
    total_score, ma_60, *_ = engineer_features(
        close_df, vol_df, universe_mask, market_close=market_close,
    )
    return close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60


def run_case(bundle, args, hybrid_tiered, rotation_trigger=None, crisis_vol=None, label=''):
    close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60 = bundle
    bt_kwargs = dict(
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
    )
    if hybrid_tiered:
        bt_kwargs['rotation_trigger_vol'] = rotation_trigger
        bt_kwargs['crisis_vol'] = crisis_vol

    bt = EventDrivenBacktester(**bt_kwargs)
    trades_df, equity_df = bt.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=args.top_k,
        threshold=2.0,
        market_close=market_close,
        vol_df=vol_df,
        universe_mask=universe_mask,
    )
    report_eq, report_tr = slice_evaluation_window(
        equity_df, trades_df,
        eval_start=args.eval_start,
        initial_capital=args.capital,
    )
    metrics = compute_risk_metrics(report_eq, report_tr, args.capital)

    avg_scale = None
    tiered_log = getattr(bt, '_tiered_scales_log', [])
    if tiered_log:
        avg_scale = float(pd.DataFrame(tiered_log)['scale'].mean())

    row = {
        'label': label,
        'hybrid_tiered': hybrid_tiered,
        'rotation_trigger': rotation_trigger,
        'crisis_vol': crisis_vol,
        'total_return_pct': round(metrics['total_return'] * 100, 2),
        'ann_return_pct': round(metrics['ann_return'] * 100, 2),
        'ann_vol_pct': round(metrics['ann_volatility'] * 100, 2),
        'sharpe': round(metrics['sharpe'], 3),
        'max_dd_pct': round(metrics['max_drawdown_pct'] * 100, 2),
        'calmar': round(metrics['calmar'], 3),
        'trades': int(metrics['total_trades']),
        'win_rate_pct': round(metrics['win_rate'] * 100, 1),
        'avg_rotation_scale': round(avg_scale, 3) if avg_scale is not None else None,
    }
    print(
        f"  {label:<28} ann {row['ann_return_pct']:+6.1f}%  "
        f"MDD {row['max_dd_pct']:+6.1f}%  Sharpe {row['sharpe']:.3f}  "
        f"rot_avg {row['avg_rotation_scale']}"
    )
    return row


def main():
    parser = argparse.ArgumentParser(description='v9 rotation_trigger × crisis_vol 參數掃描')
    parser.add_argument('--days', type=int, default=1200)
    parser.add_argument('--start-date', default=None)
    parser.add_argument('--end-date', default=None)
    parser.add_argument('--eval-start', default=None)
    parser.add_argument('--top-k', type=int, default=7)
    parser.add_argument('--hold-days', type=int, default=20)
    parser.add_argument('--tp-atr', type=float, default=4.0)
    parser.add_argument('--sl-atr', type=float, default=3.0)
    parser.add_argument('--gap-filter', type=float, default=1.5)
    parser.add_argument('--regime-floor', type=float, default=0.10)
    parser.add_argument('--capital', type=float, default=200_000)
    parser.add_argument('--position-size', type=float, default=0.10)
    parser.add_argument('--universe-size', type=int, default=60)
    parser.add_argument('--mdd-limit', type=float, default=-14.0,
                        help='MDD 必須優於此值（例如 -14 表示回撤 < 14%%）')
    args = parser.parse_args()

    bundle = load_bundle(args)

    print("\n" + "=" * 72)
    print("▶ v8.5 baseline")
    v85 = run_case(bundle, args, hybrid_tiered=False, label='v8.5 baseline')

    print("\n" + "=" * 72)
    print("▶ v9 歷史最佳參數 (trigger=15%, crisis=30%)")
    hist_best = run_case(
        bundle, args, hybrid_tiered=True,
        rotation_trigger=0.15, crisis_vol=0.30,
        label='v9 hist-best 15/30',
    )

    print("\n" + "=" * 72)
    print(f"▶ v9 新預設 (trigger={ROTATION_TRIGGER_VOL:.0%}, crisis={CRISIS_VOL:.0%})")
    v9_default = run_case(
        bundle, args, hybrid_tiered=True,
        rotation_trigger=ROTATION_TRIGGER_VOL, crisis_vol=CRISIS_VOL,
        label=f'v9 default {ROTATION_TRIGGER_VOL:.0%}/{CRISIS_VOL:.0%}',
    )

    print("\n" + "=" * 72)
    print("▶ 參數網格掃描")
    grid_rows = []
    for trigger in TRIGGER_GRID:
        for crisis in CRISIS_GRID:
            if crisis <= trigger:
                continue
            label = f'v9 trig {int(trigger*100)}/cri {int(crisis*100)}'
            grid_rows.append(run_case(
                bundle, args, hybrid_tiered=True,
                rotation_trigger=trigger, crisis_vol=crisis,
                label=label,
            ))

    all_rows = [v85, hist_best, v9_default] + grid_rows
    df = pd.DataFrame(all_rows)

    ann_bar = v85['ann_return_pct']
    mdd_bar = args.mdd_limit
    df['beats_v85_ann'] = df['ann_return_pct'] > ann_bar
    df['beats_mdd'] = df['max_dd_pct'] > mdd_bar
    df['target_hit'] = df['beats_v85_ann'] & df['beats_mdd'] & df['hybrid_tiered']

    out_csv = 'artifacts/sweep_vol_rotation.csv'
    os.makedirs('artifacts', exist_ok=True)
    df.to_csv(out_csv, index=False)

    winners = df[df['target_hit']].sort_values(
        ['ann_return_pct', 'max_dd_pct'], ascending=[False, False]
    )

    summary = {
        'created_at': datetime.now().isoformat(),
        'v85_baseline': v85,
        'v9_historical_best': hist_best,
        'v9_default_22pct': v9_default,
        'criteria': {
            'ann_return_gt_v85': ann_bar,
            'max_dd_pct_gt': mdd_bar,
        },
        'grid_results': grid_rows,
        'winners': winners.to_dict('records'),
        'winner_count': int(len(winners)),
    }
    out_json = 'artifacts/sweep_vol_rotation.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 72)
    print("📊 掃描摘要")
    print("=" * 72)
    print(f"v8.5 基準: 年化 {ann_bar:+.1f}% | MDD {v85['max_dd_pct']:+.1f}%")
    print(f"v9 歷史最佳 (15/30): 年化 {hist_best['ann_return_pct']:+.1f}% | MDD {hist_best['max_dd_pct']:+.1f}%")
    print(f"v9 新預設 (22/30):   年化 {v9_default['ann_return_pct']:+.1f}% | MDD {v9_default['max_dd_pct']:+.1f}%")
    print(f"\n篩選條件: 年化 > {ann_bar:.1f}% 且 MDD > {mdd_bar:.1f}%")
    print(f"符合組合: {len(winners)} / {len(grid_rows)}")

    if len(winners):
        print("\n🏆 符合條件的組合（依年化排序）:")
        cols = ['rotation_trigger', 'crisis_vol', 'ann_return_pct', 'max_dd_pct', 'sharpe', 'calmar']
        print(winners[cols].to_string(index=False))
    else:
        print("\n⚠️ 無組合同時滿足「年化 > v8.5」且「MDD < 14%」")
        near = df[df['hybrid_tiered']].copy()
        near['score'] = near['ann_return_pct'] - ann_bar + (near['max_dd_pct'] - mdd_bar)
        near = near.sort_values('score', ascending=False).head(5)
        print("\n最接近目標的 v9 組合:")
        print(near[['label', 'ann_return_pct', 'max_dd_pct', 'sharpe', 'avg_rotation_scale']].to_string(index=False))

    print(f"\n📁 結果已存: {out_csv} / {out_json}")


if __name__ == '__main__':
    main()