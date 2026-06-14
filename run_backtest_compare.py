#!/usr/bin/env python3
"""從零跑 v8.5 vs v9 回測對照（同一資料、同一參數，僅差 hybrid_tiered）。"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_report import EXTENDED_TICKERS
from strategy.ai_strategy import fetch_panel_data, engineer_features, build_liquid_universe
from strategy.benchmark import fetch_benchmark
from strategy.event_backtest import EventDrivenBacktester
from strategy.evaluation import slice_evaluation_window
from strategy.risk_metrics import compute_risk_metrics, format_metrics_summary


def run_pipeline(args):
    print("📥 下載資料並建構 Universe（共用）...")
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        EXTENDED_TICKERS, days=args.days,
        start_date=args.start_date, end_date=args.end_date,
    )
    universe_mask = build_liquid_universe(close_df, vol_df, top_n=args.universe_size)
    print("\n📊 下載大盤指數 (0050) 用於 regime filter...")
    bench_raw = fetch_benchmark(
        '0050', days=args.days,
        start_date=args.start_date, end_date=args.end_date,
    )
    market_close = bench_raw * bench_raw.iloc[0] if len(bench_raw) > 0 else None
    total_score, ma_60, *_ = engineer_features(
        close_df, vol_df, universe_mask, market_close=market_close,
    )
    return close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60


def run_backtest(label, hybrid_tiered, data_bundle, args):
    close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60 = data_bundle
    print(f"\n{'=' * 60}")
    print(f"▶ {label}  (hybrid_tiered={hybrid_tiered})")
    print('=' * 60)

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
    )
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
    print(format_metrics_summary(metrics))

    tiered_log = getattr(bt, '_tiered_scales_log', [])
    avg_scale = None
    if tiered_log:
        log_df = pd.DataFrame(tiered_log)
        avg_scale = float(log_df['scale'].mean())
        latest = log_df[log_df['date'] == log_df['date'].max()]
        latest_scale = float(latest['scale'].iloc[-1]) if 'scale' in latest.columns else avg_scale
        print(f"   Rotation avg scale: {avg_scale:.3f} | latest: {latest_scale:.3f}")

    return {
        'label': label,
        'hybrid_tiered': hybrid_tiered,
        'total_return_pct': metrics['total_return'] * 100,
        'ann_return_pct': metrics['ann_return'] * 100,
        'ann_vol_pct': metrics['ann_volatility'] * 100,
        'sharpe': metrics['sharpe'],
        'max_dd_pct': metrics['max_drawdown_pct'] * 100,
        'calmar': metrics['calmar'],
        'trades': metrics['total_trades'],
        'win_rate_pct': metrics['win_rate'] * 100,
        'avg_scale': avg_scale,
    }


def main():
    parser = argparse.ArgumentParser(description='v8.5 vs v9 回測對照')
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
    parser.add_argument('--capital', type=float, default=1_000_000)
    parser.add_argument('--position-size', type=float, default=0.10)
    parser.add_argument('--universe-size', type=int, default=60)
    args = parser.parse_args()

    bundle = run_pipeline(args)
    v85 = run_backtest('v8.5 baseline', False, bundle, args)
    v9 = run_backtest('v9 Hybrid Tiered', True, bundle, args)

    print(f"\n{'=' * 60}")
    print("📊 對照摘要")
    print('=' * 60)
    rows = [v85, v9]
    header = (
        f"{'版本':<18} {'總報酬':>8} {'年化':>8} {'波動':>7} {'Sharpe':>7} "
        f"{'MDD':>8} {'Calmar':>7} {'交易':>5} {'勝率':>6} {'均scale':>8}"
    )
    print(header)
    print('-' * len(header))
    for r in rows:
        scale_s = f"{r['avg_scale']:.3f}" if r['avg_scale'] is not None else '  n/a'
        print(
            f"{r['label']:<18} "
            f"{r['total_return_pct']:+7.1f}% "
            f"{r['ann_return_pct']:+7.1f}% "
            f"{r['ann_vol_pct']:6.1f}% "
            f"{r['sharpe']:7.3f} "
            f"{r['max_dd_pct']:+7.1f}% "
            f"{r['calmar']:7.2f} "
            f"{r['trades']:5.0f} "
            f"{r['win_rate_pct']:5.1f}% "
            f"{scale_s:>8}"
        )

    ann_delta = v9['ann_return_pct'] - v85['ann_return_pct']
    mdd_delta = v9['max_dd_pct'] - v85['max_dd_pct']
    print(
        f"\nΔ v9 - v8.5: 年化 {ann_delta:+.1f}pp | MDD {mdd_delta:+.1f}pp | "
        f"Sharpe {v9['sharpe'] - v85['sharpe']:+.3f}"
    )


if __name__ == '__main__':
    main()