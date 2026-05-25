#!/usr/bin/env python3
"""
板塊輪動策略 v2 — Sector Rotation Report

美股 Macro Regime (SPY/VIX/SOX) + 板塊資金流 + 板塊內選股。
完全獨立於 v8.5 的動量策略。

使用方式:
  python3 sector_rotation_report.py                     # 預設 1200 天
  python3 sector_rotation_report.py --start-date 2019-01-01  # 7 年回測
  python3 sector_rotation_report.py --compare            # 與 0050 比較
"""

import argparse
import sys
import numpy as np
import pandas as pd
from datetime import datetime

# 沿用 v8.5 的數據和工具
from strategy.ai_strategy import fetch_panel_data, build_liquid_universe
from strategy.us_market import fetch_us_signals, align_us_to_tw
from strategy.sector_rotation_backtest import SectorRotationBacktester
from strategy.evaluation import slice_evaluation_window
from strategy.risk_metrics import compute_risk_metrics, format_metrics_summary
from strategy.benchmark import fetch_benchmark

# 沿用 v8.5 的股池
EXTENDED_TICKERS = None  # 會從 ai_report.py 導入


def get_tickers():
    """取得擴展股池。"""
    try:
        from ai_report import EXTENDED_TICKERS
        return EXTENDED_TICKERS
    except ImportError:
        # Fallback: 主要台股
        return [
            '2330', '2317', '2454', '2308', '2382', '2412', '2881', '2882',
            '2884', '2886', '2891', '2892', '2303', '2327', '3711', '2345',
            '3008', '2357', '6505', '1301', '1303', '1326', '2002', '2105',
            '2207', '2301', '2395', '2408', '2474', '2603', '2609', '2615',
            '2801', '2880', '2883', '2885', '2887', '2888', '2890', '3034',
            '3037', '3443', '3481', '3529', '4904', '4938', '5871', '5880',
            '6446', '6669', '8046', '9910', '2049', '3231', '5269', '6239',
            '2344', '3661', '6409', '2379', '6415', '3017', '2383', '8150',
            '3706', '6531', '2347', '6547', '2385', '3023', '2377', '6285',
            '2492', '3665', '2404', '5264', '3653', '6592', '2376', '6770',
            '6443', '6550', '2353', '3714', '4966', '3406', '6269', '4977',
            '3533', '3035', '2618', '3702', '6414', '5876', '3036', '2634',
            '3044', '6789', '2542', '6196', '2059', '2006', '1590', '2360',
            '3682', '6166', '3056', '8069', '6271', '6588', '6552', '3105',
            '3032', '2441',
        ]


def parse_args():
    parser = argparse.ArgumentParser(
        description='板塊輪動策略 v2 — Sector Rotation'
    )
    parser.add_argument('--days', type=int, default=1200,
                        help='回測天數 (預設 1200)')
    parser.add_argument('--start-date', type=str, default=None,
                        help='起始日期 (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, default=None,
                        help='結束日期 (YYYY-MM-DD)')
    parser.add_argument('--eval-start', type=str, default=None,
                        help='績效評估起始日期 (YYYY-MM-DD)，可晚於資料暖機起始日')
    parser.add_argument('--top-sectors', type=int, default=3,
                        help='選取幾個板塊 (預設 3)')
    parser.add_argument('--stocks-per-sector', type=int, default=3,
                        help='每板塊選幾檔 (預設 3)')
    parser.add_argument('--hold-days', type=int, default=20,
                        help='最大持倉天數 (預設 20)')
    parser.add_argument('--tp-atr', type=float, default=4.0,
                        help='ATR 停利倍數 (預設 4.0)')
    parser.add_argument('--sl-atr', type=float, default=3.0,
                        help='ATR 停損倍數 (預設 3.0)')
    parser.add_argument('--universe-size', type=int, default=60,
                        help='流動性 Universe 大小 (預設 60)')
    parser.add_argument('--compare', action='store_true',
                        help='與 0050 benchmark 比較')
    parser.add_argument('--capital', type=float, default=1_000_000,
                        help='起始資金 (預設 1,000,000)')
    return parser.parse_args()


def main():
    args = parse_args()
    tickers = get_tickers()

    print("=" * 60)
    print("🔄 板塊輪動策略 v2 — Sector Rotation")
    print("=" * 60)
    print(f"   美股前提: SPY/VIX (Macro Regime) + SOX (Tech Gate)")
    print(f"   板塊選擇: 10/15/20d 資金流 Top-{args.top_sectors}")
    print(f"   板塊內選股: 每板塊 Top-{args.stocks_per_sector}")
    print(f"   出場: ATR TP={args.tp_atr} / SL={args.sl_atr}, 持倉 {args.hold_days} 天")
    if args.start_date:
        print(f"   回測期間: {args.start_date} → {args.end_date or '今天'}")
    else:
        print(f"   回測天數: {args.days}")
    print("=" * 60)

    # Phase 1: 台股數據
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        tickers, days=args.days,
        start_date=args.start_date, end_date=args.end_date,
    )

    # Phase 2: 動態 Universe
    universe_mask = build_liquid_universe(close_df, vol_df, top_n=args.universe_size)

    # Phase 3: 美股信號
    us_signals = fetch_us_signals(
        start_date=args.start_date or close_df.index[0].strftime('%Y-%m-%d'),
        end_date=args.end_date or close_df.index[-1].strftime('%Y-%m-%d'),
    )
    us_aligned = align_us_to_tw(us_signals, close_df.index)

    # Phase 4: 回測
    backtester = SectorRotationBacktester(
        initial_capital=args.capital,
        tp_atr_mult=args.tp_atr,
        sl_atr_mult=args.sl_atr,
        max_hold_days=args.hold_days,
        top_sectors=args.top_sectors,
        stocks_per_sector=args.stocks_per_sector,
    )

    print(f"\n🔄 執行板塊輪動回測...")
    trades_df, equity_df = backtester.run(
        close_df, open_df, high_df, low_df, vol_df,
        us_aligned, universe_mask,
    )

    report_equity_df, report_trades_df = slice_evaluation_window(
        equity_df, trades_df,
        eval_start=args.eval_start,
        initial_capital=args.capital,
    )
    if args.eval_start:
        print(f"\n📏 績效統計區間: {args.eval_start} → {args.end_date or '今天'} "
              f"(資料自 {args.start_date or f'近 {args.days} 天'} 暖機)")

    # Phase 5: 風險指標
    if not report_equity_df.empty:
        metrics = compute_risk_metrics(report_equity_df, report_trades_df, args.capital)
        print(format_metrics_summary(metrics))

        # 板塊分布統計
        if 'Sector' in report_trades_df.columns:
            print("\n📊 交易板塊分布:")
            sector_stats = report_trades_df.groupby('Sector').agg(
                trades=('Return_Pct', 'count'),
                avg_ret=('Return_Pct', 'mean'),
                win_rate=('Return_Pct', lambda x: (x > 0).mean()),
            )
            sector_stats = sector_stats.sort_values('trades', ascending=False)
            for sector, row in sector_stats.iterrows():
                print(f"   {sector:20s}: {row['trades']:>4.0f} 筆 | "
                      f"均報酬 {row['avg_ret']*100:>+5.1f}% | "
                      f"勝率 {row['win_rate']*100:>4.0f}%")

        # 出場原因分布
        if 'Reason' in report_trades_df.columns:
            print("\n📊 出場原因:")
            for reason, count in report_trades_df['Reason'].value_counts().items():
                pct = count / len(report_trades_df) * 100
                avg_ret = report_trades_df[report_trades_df['Reason'] == reason]['Return_Pct'].mean()
                print(f"   {reason}: {count} 筆 ({pct:.0f}%) | "
                      f"均報酬 {avg_ret*100:+.1f}%")

    else:
        print("❌ 沒有可評估的 equity 資料")
        return

    # Phase 6: Benchmark 比較
    if args.compare:
        print("\n📊 Benchmark 比較:")
        benchmark_start = args.eval_start or args.start_date
        benchmark_equity = fetch_benchmark(
            '0050', days=args.days,
            start_date=benchmark_start, end_date=args.end_date,
        )
        if benchmark_equity is not None and not benchmark_equity.empty:
            bm_ret = (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0] - 1) * 100
            bm_daily = benchmark_equity.pct_change().dropna()
            bm_sharpe = bm_daily.mean() / bm_daily.std() * np.sqrt(252)
            bm_peak = benchmark_equity.cummax()
            bm_mdd = ((benchmark_equity - bm_peak) / bm_peak).min() * 100

            strat_ret = (report_equity_df['Equity'].iloc[-1] / args.capital - 1) * 100

            print(f"   {'':20s} | {'策略':>10s} | {'0050':>10s}")
            print(f"   {'─'*50}")
            print(f"   {'總報酬':20s} | {strat_ret:>+9.1f}% | {bm_ret:>+9.1f}%")
            print(f"   {'Sharpe':20s} | {metrics.get('sharpe', 0):>10.2f} | {bm_sharpe:>10.2f}")
            print(f"   {'MDD':20s} | {metrics.get('max_drawdown_pct', 0)*100:>9.1f}% | {bm_mdd:>9.1f}%")

    # 存 equity CSV
    if not report_equity_df.empty:
        import os
        os.makedirs('artifacts', exist_ok=True)
        today = datetime.now().strftime('%Y%m%d')
        report_equity_df.to_csv(f'artifacts/sr_equity_{today}.csv')
        if not report_trades_df.empty:
            report_trades_df.to_csv(f'artifacts/sr_trades_{today}.csv', index=False)
        print(f"\n💾 已儲存 artifacts/sr_equity_{today}.csv")

    print("\n🚀 板塊輪動回測完成！")


if __name__ == '__main__':
    main()
