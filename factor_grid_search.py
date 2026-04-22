#!/usr/bin/env python3
"""
因子組合搜索引擎 (Factor Grid Search Engine)

仿 FinLab sim_conditions() 的概念，搜索 FinLab 啟發因子
（RSI、創新高、價值）與現有因子的最佳組合。

三種模式：
  1. ablation  — 單因子貢獻分析（含 baseline 對比）
  2. compare   — 最佳組合 vs v8.5 baseline
  3. grid      — 完整因子×權重網格搜索

使用方式：
    python factor_grid_search.py --mode ablation    # 因子消融
    python factor_grid_search.py --mode compare     # 最佳 vs baseline
    python factor_grid_search.py --mode grid        # 完整網格（耗時）
"""

import argparse
import os
import sys
from datetime import datetime
from itertools import combinations

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.ai_strategy import fetch_panel_data, build_liquid_universe
from strategy.event_backtest import EventDrivenBacktester
from strategy.risk_metrics import compute_risk_metrics
from strategy.finlab_factors import (
    compute_rsi_rank,
    compute_breakout_rank,
    compute_value_rank,
    compute_revenue_momentum,
)


DEFAULT_TICKERS = [
    '2330', '2317', '2454', '2308', '2881',
    '2603', '3231', '3481', '2382', '2609',
    '2891', '1519', '2379', '2303',
]

EXTENDED_TICKERS = [
    '2330', '2454', '2303', '3711', '2379', '6770', '3034', '2449',
    '5274', '3529', '2408', '3443', '3035', '6415', '6525', '3661',
    '3037', '2344', '6547',
    '2317', '2382', '2308', '2301', '2357', '2376', '2395', '3231',
    '2474', '2353', '3481', '3017', '2345', '2383', '2356', '3044',
    '2327', '3036', '2324', '2377', '2385', '2360', '2404',
    '2412', '2459', '2458', '3045', '6505', '3023',
    '3706', '3533', '2368', '4904', '4938', '6669',
    '2881', '2882', '2884', '2886', '2887', '2891', '2892',
    '2880', '2883', '2885', '2888', '2889', '2890', '5880', '5876',
    '2801', '2834', '2838', '2845', '2855', '2867', '2897',
    '1301', '1303', '1326', '2002', '1101', '1102', '2912',
    '1216', '2207', '9904', '1402', '9910', '1605', '2603',
    '2609', '2615', '1519', '2606', '6005',
    '2618', '2610', '2605', '2634', '2637',
    '4142', '1760', '6446', '1707', '4743',
    '9945', '8454', '1504', '2105', '2201', '2204',
    '5871', '6116', '6285', '3149', '6239',
]
EXTENDED_TICKERS = list(dict.fromkeys(EXTENDED_TICKERS))


def compute_all_factors(close_df, vol_df, universe_mask=None, skip_value=True):
    """
    計算所有候選因子（現有 + FinLab 新增）。

    Returns
    -------
    factors : dict[str, pd.DataFrame]
        因子名稱 → 橫截面排名 DataFrame
    """
    print("🧮 計算所有候選因子...")

    # === 現有因子 ===
    mom_20 = close_df / close_df.shift(20)
    ma_60 = close_df.rolling(60).mean()
    trend_bias = close_df / ma_60

    def _rank(df):
        if universe_mask is not None:
            return df.where(universe_mask).rank(axis=1, pct=True)
        return df.rank(axis=1, pct=True)

    factors = {
        'momentum_20': _rank(mom_20),
        'trend_60ma': _rank(trend_bias),
    }

    # === FinLab 新增因子 ===
    print("   📈 RSI-20 動量排名...")
    factors['rsi_20'] = compute_rsi_rank(close_df, period=20, universe_mask=universe_mask)

    print("   🏔️ 300 日創新高突破...")
    factors['breakout_300'] = compute_breakout_rank(close_df, window=300, universe_mask=universe_mask)

    print("   📊 60 日營收動能代理...")
    factors['rev_momentum_60'] = compute_revenue_momentum(close_df, period=60, universe_mask=universe_mask)

    # 價值因子需要 API 呼叫，可選跳過
    if not skip_value:
        try:
            print("   💰 PE/PB 價值因子...")
            factors['value_pb_pe'] = compute_value_rank(
                close_df, universe_mask=universe_mask,
                tickers=list(close_df.columns),
            )
        except Exception as e:
            print(f"   ⚠️ 價值因子取得失敗: {e}")

    print(f"   ✅ 共 {len(factors)} 個因子計算完成")
    return factors, ma_60


def run_factor_combination(factors, weights, close_df, open_df, high_df, low_df,
                           vol_df, ma_60, universe_mask, market_close,
                           initial_capital=1_000_000, top_k=7, hold_days=20):
    """
    跑一組因子組合的回測，返回績效指標。

    Parameters
    ----------
    factors : dict[str, pd.DataFrame]
        因子名稱 → 排名 DataFrame
    weights : dict[str, float]
        因子名稱 → 權重

    Returns
    -------
    metrics : dict
    """
    # 計算加權總分
    total_score = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
    for name, w in weights.items():
        if name in factors and w > 0:
            total_score = total_score + factors[name] * w

    # 計算 threshold：排名加權後的合理門檻
    active_weight_sum = sum(w for w in weights.values() if w > 0)
    threshold = active_weight_sum * 0.5 if active_weight_sum > 0 else 0

    # ATR
    atr_df = close_df.pct_change().abs().rolling(20).mean() * close_df

    # 回測（對齊 v8.5 主配置）
    backtester = EventDrivenBacktester(
        tp_pct=0.15,
        sl_pct=0.08,
        max_hold_days=hold_days,
        initial_capital=initial_capital,
        position_size=0.10,
        tp_sl_mode='atr',
        tp_atr_mult=4.0,
        sl_atr_mult=3.0,
        gap_filter_atr=1.5,
        regime_filter=True,
        regime_graduated=True,
        regime_floor=0.10,
        breadth_regime=True,
        gap_aware_sizing=True,
        corr_filter=0.8,
        sector_max_pct=0.75,
        slippage=0.001,
        buy_cost=0.001425,
        sell_cost=0.004425,
    )

    trades_df, equity_df = backtester.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=top_k,
        threshold=threshold,
        market_close=market_close,
        vol_df=vol_df,
        universe_mask=universe_mask,
    )

    metrics = compute_risk_metrics(equity_df, trades_df, initial_capital)
    return metrics, equity_df


def run_ablation(close_df, open_df, high_df, low_df, vol_df,
                 universe_mask, market_close, args):
    """
    單因子消融分析：每個因子獨立跑，再跑組合。

    仿 FinLab 官方 6 步流程的步驟 2-3。
    """
    factors, ma_60 = compute_all_factors(
        close_df, vol_df, universe_mask, skip_value=args.skip_value
    )

    configs = []

    # === Baseline（v8.5 現有配置）===
    configs.append(('v8.5 Baseline (mom×3+trend×1)', {
        'momentum_20': 3.0, 'trend_60ma': 1.0
    }))

    # === 單因子測試（FinLab 新因子）===
    configs.append(('RSI-20 only', {'rsi_20': 1.0}))
    configs.append(('Breakout-300 only', {'breakout_300': 1.0}))
    configs.append(('RevMomentum-60 only', {'rev_momentum_60': 1.0}))
    if 'value_pb_pe' in factors:
        configs.append(('Value (PB+PE) only', {'value_pb_pe': 1.0}))

    # === 現有因子 + 各新因子 ===
    configs.append(('Baseline + RSI×1', {
        'momentum_20': 3.0, 'trend_60ma': 1.0, 'rsi_20': 1.0
    }))
    configs.append(('Baseline + Breakout×1', {
        'momentum_20': 3.0, 'trend_60ma': 1.0, 'breakout_300': 1.0
    }))
    configs.append(('Baseline + RevMom×1', {
        'momentum_20': 3.0, 'trend_60ma': 1.0, 'rev_momentum_60': 1.0
    }))
    if 'value_pb_pe' in factors:
        configs.append(('Baseline + Value×1', {
            'momentum_20': 3.0, 'trend_60ma': 1.0, 'value_pb_pe': 1.0
        }))

    # === FinLab 雙渦輪風格（價值+動量）===
    configs.append(('FinLab 雙渦輪 (Mom+RevMom+Trend)', {
        'momentum_20': 2.0, 'rev_momentum_60': 2.0, 'trend_60ma': 1.0
    }))

    # === 全因子 ensemble ===
    all_weights = {'momentum_20': 3.0, 'trend_60ma': 1.0,
                   'rsi_20': 0.5, 'breakout_300': 0.5, 'rev_momentum_60': 0.5}
    if 'value_pb_pe' in factors:
        all_weights['value_pb_pe'] = 0.5
    configs.append(('All Factors Ensemble', all_weights))

    # === 執行所有配置 ===
    results = []
    equity_curves = {}

    for label, weights in configs:
        print(f"\n--- {label} ---")
        # 確保所有用到的因子都存在
        valid_weights = {k: v for k, v in weights.items() if k in factors}
        if not valid_weights:
            print(f"   ⚠️ 無可用因子，跳過")
            continue

        try:
            metrics, eq_df = run_factor_combination(
                factors, valid_weights, close_df, open_df, high_df, low_df,
                vol_df, ma_60, universe_mask, market_close,
                initial_capital=args.capital, top_k=args.top_k,
                hold_days=args.hold_days,
            )
            metrics['label'] = label
            metrics['weights'] = str(valid_weights)
            results.append(metrics)
            equity_curves[label] = eq_df
        except Exception as e:
            print(f"   ❌ 失敗: {e}")
            import traceback
            traceback.print_exc()

    return results, equity_curves


def print_results_table(results):
    """輸出結果比較表。"""
    print("\n\n" + "=" * 100)
    print("📊 因子組合搜索結果 (Factor Grid Search Results)")
    print("=" * 100)

    # 找到 baseline
    baseline = next((r for r in results if 'Baseline' in r.get('label', '')
                     and '+' not in r.get('label', '')), None)
    baseline_sharpe = baseline['sharpe'] if baseline else 0

    header = (f"{'Configuration':<40s} | {'Sharpe':>7s} | {'Ann%':>7s} | "
              f"{'MDD%':>7s} | {'Calmar':>7s} | {'#Tr':>4s} | "
              f"{'WR%':>5s} | {'PF':>5s} | {'vs BL':>7s}")
    sep = '-' * len(header)
    print(header)
    print(sep)

    for r in sorted(results, key=lambda x: x.get('sharpe', 0), reverse=True):
        label = r.get('label', '')[:40]
        sharpe = r.get('sharpe', 0)
        ann = r.get('ann_return', 0) * 100
        mdd = r.get('max_drawdown_pct', 0) * 100
        calmar = r.get('calmar', 0)
        trades = r.get('total_trades', 0)
        wr = r.get('win_rate', 0) * 100
        pf = r.get('profit_factor', 0)
        delta = sharpe - baseline_sharpe

        marker = ''
        if 'Baseline' in r.get('label', '') and '+' not in r.get('label', ''):
            marker = ' ⭐'
        elif delta > 0.05:
            marker = ' 🆕'
        elif delta < -0.3:
            marker = ' ☠️'
        elif delta < -0.1:
            marker = ' 🔴'

        print(f"{label:<40s} | {sharpe:>7.3f} | {ann:>+6.1f}% | "
              f"{mdd:>6.1f}% | {calmar:>7.3f} | {trades:>4d} | "
              f"{wr:>4.1f}% | {pf:>5.2f} | {delta:>+6.3f}{marker}")

    print(sep)


def plot_results(results, equity_curves, output_path='factor_search_chart.png'):
    """繪製因子搜索結果圖表。"""
    plt.style.use('dark_background')
    fig, axes = plt.subplots(1, 2, figsize=(20, 8))

    # 左圖：資金曲線
    ax1 = axes[0]
    colors = ['#00e5ff', '#ffab00', '#ab47bc', '#ff4444', '#00ff00',
              '#ff9800', '#e91e63', '#00bcd4', '#8bc34a', '#ffc107',
              '#9c27b0', '#4caf50']
    for i, (label, eq_df) in enumerate(equity_curves.items()):
        lw = 2.5 if 'Baseline' in label else 1.5
        ls = '-' if 'Baseline' in label else '--'
        ax1.plot(eq_df.index, eq_df['Equity'], lw=lw, ls=ls,
                 label=label[:30], color=colors[i % len(colors)])

    ax1.set_title('Factor Combination Equity Curves', fontweight='bold',
                  fontsize=13, color='#fff')
    ax1.set_ylabel('Portfolio Value (TWD)')
    ax1.legend(fontsize=7, loc='upper left')
    ax1.grid(alpha=0.15)

    # 右圖：Sharpe 比較
    ax2 = axes[1]
    labels = [r.get('label', '')[:25] for r in results]
    sharpes = [r.get('sharpe', 0) for r in results]
    bar_colors = ['#00e5ff' if s > 0 else '#ff4444' for s in sharpes]

    # 高亮 baseline
    for i, r in enumerate(results):
        if 'Baseline' in r.get('label', '') and '+' not in r.get('label', ''):
            bar_colors[i] = '#ffab00'

    bars = ax2.barh(range(len(labels)), sharpes, color=bar_colors, alpha=0.8)
    ax2.set_yticks(range(len(labels)))
    ax2.set_yticklabels(labels, fontsize=8)
    ax2.set_xlabel('Sharpe Ratio')
    ax2.set_title('Sharpe Ratio: Factor Combinations', fontweight='bold',
                  fontsize=13, color='#fff')
    ax2.axvline(0, color='#555', ls='--', alpha=0.5)
    ax2.grid(alpha=0.15, axis='x')

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='#121212')
    plt.close(fig)
    print(f"\n📈 圖表已存為 {output_path}")


def main():
    parser = argparse.ArgumentParser(description='因子組合搜索引擎')
    parser.add_argument('--mode', choices=['ablation', 'compare', 'grid'],
                        default='ablation', help='搜索模式')
    parser.add_argument('--days', type=int, default=1200,
                        help='回測天數（預設 1200）')
    parser.add_argument('--capital', type=float, default=1_000_000)
    parser.add_argument('--top-k', type=int, default=7)
    parser.add_argument('--hold-days', type=int, default=20)
    parser.add_argument('--static-pool', action='store_true',
                        help='使用靜態股池（預設動態 Universe）')
    parser.add_argument('--skip-value', action='store_true', default=True,
                        help='跳過價值因子（避免大量 API 呼叫）')
    parser.add_argument('--no-skip-value', dest='skip_value',
                        action='store_false',
                        help='啟用價值因子（需要 yfinance API 呼叫）')
    parser.add_argument('--output', type=str, default='factor_search_results.csv',
                        help='結果 CSV 輸出路徑')
    args = parser.parse_args()

    print("=" * 60)
    print("🔬 因子組合搜索引擎 (FinLab-Inspired Factor Grid Search)")
    print("=" * 60)
    print(f"   模式: {args.mode}")
    print(f"   回測天數: {args.days}")
    print(f"   Top-K: {args.top_k}")
    print(f"   價值因子: {'啟用' if not args.skip_value else '跳過'}")
    print("=" * 60)

    # 下載資料
    tickers = DEFAULT_TICKERS if args.static_pool else EXTENDED_TICKERS
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        tickers, days=args.days
    )

    # 動態 Universe
    universe_mask = None if args.static_pool else \
        build_liquid_universe(close_df, vol_df, top_n=60)

    # 下載 0050 (regime filter)
    from strategy.benchmark import fetch_benchmark
    market_close = fetch_benchmark('0050', days=args.days)
    if len(market_close) > 0:
        market_close = market_close * market_close.iloc[0]
    else:
        market_close = None
        print("⚠️ 無法下載 0050，regime filter 停用")

    # 執行搜索
    if args.mode == 'ablation':
        results, equity_curves = run_ablation(
            close_df, open_df, high_df, low_df, vol_df,
            universe_mask, market_close, args,
        )
    elif args.mode in ('compare', 'grid'):
        # compare/grid 模式先跑 ablation，後續可擴展
        results, equity_curves = run_ablation(
            close_df, open_df, high_df, low_df, vol_df,
            universe_mask, market_close, args,
        )
    else:
        print(f"❌ 未知模式: {args.mode}")
        return

    if not results:
        print("❌ 無回測結果")
        return

    # 輸出結果
    print_results_table(results)

    # 繪圖
    plot_results(results, equity_curves)

    # 存 CSV
    summary_rows = []
    for r in results:
        summary_rows.append({
            'Configuration': r.get('label', ''),
            'Weights': r.get('weights', ''),
            'Sharpe': round(r.get('sharpe', 0), 3),
            'Ann_Return_%': round(r.get('ann_return', 0) * 100, 2),
            'MDD_%': round(r.get('max_drawdown_pct', 0) * 100, 1),
            'Calmar': round(r.get('calmar', 0), 3),
            'Trades': r.get('total_trades', 0),
            'Win_Rate_%': round(r.get('win_rate', 0) * 100, 1),
            'Profit_Factor': round(r.get('profit_factor', 0), 2),
        })
    pd.DataFrame(summary_rows).to_csv(args.output, index=False)
    print(f"📄 結果已存為 {args.output}")

    # 結論
    baseline = next((r for r in results if 'Baseline' in r.get('label', '')
                     and '+' not in r.get('label', '')), None)
    best = max(results, key=lambda x: x.get('sharpe', 0))

    if baseline and best:
        bl_sharpe = baseline['sharpe']
        best_sharpe = best['sharpe']
        if best['label'] == baseline['label']:
            print("\n✅ v8.5 Baseline 仍為最優，新因子無法超越")
        elif best_sharpe > bl_sharpe * 1.05:
            print(f"\n🆕 發現更優組合: {best['label']}")
            print(f"   Sharpe {best_sharpe:.3f} vs baseline {bl_sharpe:.3f} "
                  f"(+{(best_sharpe/bl_sharpe - 1)*100:.1f}%)")
            print(f"   ⚠️ 需經 walk_forward.py + monte_carlo.py 驗證後才能採用")
        else:
            print(f"\n⚠️ 最佳組合 ({best['label']}) 與 baseline 差異 < 5%，"
                  f"建議維持現有配置")


if __name__ == '__main__':
    main()
