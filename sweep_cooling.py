#!/usr/bin/env python3
"""掃描冷卻輪動參數：波動回落賣 Core alpha → Satellite 加碼。"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_vol_rotation import load_bundle, run_case


def run_v9(bundle, args, label, **extra):
    from strategy.event_backtest import EventDrivenBacktester
    from strategy.evaluation import slice_evaluation_window
    from strategy.risk_metrics import compute_risk_metrics

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
        hybrid_tiered=True,
        core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15,
        rotation_trigger_vol=args.rotation_trigger,
        crisis_vol=args.crisis_vol,
        buy_cost=0.001425,
        sell_cost=0.004425,
        **extra,
    )
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
    row = {
        'label': label,
        'ann_return_pct': round(metrics['ann_return'] * 100, 2),
        'max_dd_pct': round(metrics['max_drawdown_pct'] * 100, 2),
        'sharpe': round(metrics['sharpe'], 3),
        'trades': int(metrics['total_trades']),
        **{k: extra.get(k) for k in extra},
    }
    print(
        f"  {label:<36} ann {row['ann_return_pct']:+6.1f}%  "
        f"MDD {row['max_dd_pct']:+6.1f}%  Sharpe {row['sharpe']:.3f}  "
        f"交易 {row['trades']}"
    )
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--capital', type=float, default=200_000)
    p.add_argument('--days', type=int, default=1200)
    p.add_argument('--rotation-trigger', type=float, default=0.22)
    p.add_argument('--crisis-vol', type=float, default=0.35)
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
    v85 = run_case(bundle, args, False, label='v8.5')
    ann_bar = v85['ann_return_pct']
    mdd_bar = v85['max_dd_pct']

    print(f"\n基準 v8.5: ann {ann_bar:+.1f}%  MDD {mdd_bar:+.1f}%\n")

    grid = []
    sat_boosts = [1.70, 1.90, 2.10, 2.30]
    cooling_days_list = [8, 12, 16]
    trim_fracs = [0.50, 0.70, 0.85]
    min_pnls = [0.003, 0.005, 0.008]

    for sat_b in sat_boosts:
        for cd in cooling_days_list:
            for tf in trim_fracs:
                for mp in min_pnls:
                    label = f"sat{sat_b:.2f} d{cd} trim{int(tf*100)} mp{mp:.3f}"
                    grid.append(run_v9(
                        bundle, args, label,
                        cooling_sat_boost=sat_b,
                        cooling_days=cd,
                        core_alpha_trim_frac=tf,
                        core_alpha_trim_min_pnl=mp,
                    ))

    df = pd.DataFrame(grid)
    df['beats_ann'] = df['ann_return_pct'] > ann_bar
    df['beats_mdd'] = df['max_dd_pct'] > mdd_bar
    df['dual_win'] = df['beats_ann'] & df['beats_mdd']
    df['score'] = (
        (df['ann_return_pct'] - ann_bar)
        + (df['max_dd_pct'] - mdd_bar) * 0.5
    )

    winners = df[df['dual_win']].sort_values('ann_return_pct', ascending=False)
    print("\n" + "=" * 72)
    print(f"雙贏組合: {len(winners)} / {len(df)}")
    if len(winners):
        print(winners.head(10).to_string(index=False))
    else:
        print("\n最接近雙贏:")
        print(df.sort_values('score', ascending=False).head(8).to_string(index=False))

    os.makedirs('artifacts', exist_ok=True)
    df.to_csv('artifacts/sweep_cooling.csv', index=False)
    print("\n📁 artifacts/sweep_cooling.csv")


if __name__ == '__main__':
    main()