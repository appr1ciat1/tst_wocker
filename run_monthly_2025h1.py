#!/usr/bin/env python3
"""2025-01 ~ 2025-06 每月表現：v8.5 vs v9（關稅衝擊區間）。"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_report import EXTENDED_TICKERS
from strategy.ai_strategy import fetch_panel_data, engineer_features, build_liquid_universe
from strategy.benchmark import fetch_benchmark
from strategy.event_backtest import EventDrivenBacktester
from strategy.portfolio_vol_target import (
    ROTATION_TRIGGER_VOL, CRISIS_VOL, TARGET_ANN_VOL_DEFAULT,
)

START = '2025-01-01'
END = '2025-06-30'
CAPITAL = 200_000


def run_bt(hybrid_tiered: bool):
    days = 1500
    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        EXTENDED_TICKERS, days=days, end_date=END,
    )
    universe_mask = build_liquid_universe(close_df, vol_df, top_n=60)
    bench_raw = fetch_benchmark('0050', days=days, end_date=END)
    market_close = bench_raw * bench_raw.iloc[0] if len(bench_raw) > 0 else None
    total_score, ma_60, *_ = engineer_features(
        close_df, vol_df, universe_mask, market_close=market_close,
    )
    kw = dict(
        tp_sl_mode='atr', tp_atr_mult=4.0, sl_atr_mult=3.0,
        max_hold_days=20, initial_capital=CAPITAL, position_size=0.10,
        regime_filter=True, regime_graduated=True, regime_floor=0.10,
        gap_filter_atr=1.5, breadth_regime=True,
        hybrid_tiered=hybrid_tiered,
        core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=TARGET_ANN_VOL_DEFAULT,
        rotation_trigger_vol=ROTATION_TRIGGER_VOL,
        crisis_vol=CRISIS_VOL,
        buy_cost=0.001425, sell_cost=0.004425,
    )
    bt = EventDrivenBacktester(**kw)
    trades_df, equity_df = bt.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=7, threshold=2.0,
        market_close=market_close, vol_df=vol_df, universe_mask=universe_mask,
    )
    return trades_df, equity_df, bt, bench_raw


def month_stats(equity: pd.Series, trades: pd.DataFrame, month: pd.Period) -> dict:
    m_start = month.start_time
    m_end = month.end_time
    seg = equity.loc[(equity.index >= m_start) & (equity.index <= m_end)]
    if len(seg) < 2:
        return None

    ret = seg.iloc[-1] / seg.iloc[0] - 1
    daily = seg.pct_change().dropna()
    vol_ann = daily.std() * np.sqrt(252) if len(daily) > 1 else 0
    dd = seg / seg.cummax() - 1
    mdd = dd.min()

    tr = trades.copy()
    if not tr.empty:
        tr['Exit_Date'] = pd.to_datetime(tr['Exit_Date'], errors='coerce')
        tr_m = tr.loc[
            (tr['Exit_Date'] >= m_start) & (tr['Exit_Date'] <= m_end)
        ]
        n_trades = len(tr_m)
        win_rate = (tr_m['Return_Pct'] > 0).mean() if n_trades else np.nan
        avg_ret = tr_m['Return_Pct'].mean() if n_trades else np.nan
        reasons = tr_m['Reason'].value_counts().head(3).to_dict() if n_trades else {}
    else:
        n_trades, win_rate, avg_ret, reasons = 0, np.nan, np.nan, {}

    return {
        'month': str(month),
        'return_pct': round(ret * 100, 2),
        'mdd_pct': round(mdd * 100, 2),
        'vol_ann_pct': round(vol_ann * 100, 2),
        'trades': int(n_trades),
        'win_rate_pct': round(win_rate * 100, 1) if n_trades else None,
        'avg_trade_pct': round(avg_ret * 100, 2) if n_trades else None,
        'top_exit_reasons': reasons,
    }


def v9_regime_days(bt, equity_index, month: pd.Period) -> dict:
    log = getattr(bt, '_tiered_scales_log', [])
    if not log:
        return {}
    df = pd.DataFrame(log)
    df['date'] = pd.to_datetime(df['date'])
    m_start, m_end = month.start_time, month.end_time
    sub = df.loc[(df['date'] >= m_start) & (df['date'] <= m_end)]
    if sub.empty:
        return {}
    # 每日 regime（取 satellite 列或任一）
    daily = sub.groupby('date').agg(
        vol_regime=('vol_regime', 'first'),
        fvol=('fvol', 'first'),
        sat_boost=('rotation_boost', 'max'),
    )
    counts = daily['vol_regime'].value_counts().to_dict()
    return {
        'regime_days': counts,
        'avg_fvol_pct': round(daily['fvol'].mean() * 100, 1),
        'max_fvol_pct': round(daily['fvol'].max() * 100, 1),
    }


def bench_monthly(bench_raw: pd.Series) -> pd.Series:
    s = bench_raw.copy()
    s.index = pd.to_datetime(s.index)
    s = s.loc[(s.index >= START) & (s.index <= END)]
    monthly = s.resample('ME').last().pct_change()
    monthly.index = monthly.index.to_period('M')
    return monthly * 100


def main():
    print('📥 回測 v8.5 ...')
    tr85, eq85, _, bench_raw = run_bt(False)
    print('📥 回測 v9 ...')
    tr9, eq9, bt9, _ = run_bt(True)

    for label, eq in [('v8.5', eq85), ('v9', eq9)]:
        eq = eq.copy()
        eq.index = pd.to_datetime(eq.index)
        eq = eq['Equity'].loc[(eq.index >= START) & (eq.index <= END)]

    eq85s = eq85.copy()
    eq85s.index = pd.to_datetime(eq85s.index)
    eq85s = eq85s['Equity'].loc[(eq85s.index >= START) & (eq85s.index <= END)]
    eq9s = eq9.copy()
    eq9s.index = pd.to_datetime(eq9s.index)
    eq9s = eq9s['Equity'].loc[(eq9s.index >= START) & (eq9s.index <= END)]

    bm = bench_monthly(bench_raw)
    months = pd.period_range('2025-01', '2025-06', freq='M')

    rows = []
    print('\n' + '=' * 100)
    print('2025-01 ~ 2025-06 每月表現（關稅衝擊區間）')
    print('=' * 100)
    hdr = f"{'月份':<8} {'0050':>7} {'v8.5月報酬':>10} {'v8.5MDD':>8} {'v9月報酬':>10} {'v9MDD':>8} {'v9交易':>6} {'v9勝率':>7} {'v9均筆':>8}"
    print(hdr)
    print('-' * 100)

    for m in months:
        s85 = month_stats(eq85s, tr85, m)
        s9 = month_stats(eq9s, tr9, m)
        reg = v9_regime_days(bt9, eq9s.index, m)
        bm_ret = bm.get(m, np.nan)
        if s85 is None or s9 is None:
            continue
        row = {
            'month': str(m),
            'bench_0050_pct': round(bm_ret, 2) if not pd.isna(bm_ret) else None,
            **{f'v85_{k}': v for k, v in s85.items() if k != 'month'},
            **{f'v9_{k}': v for k, v in s9.items() if k != 'month'},
            'v9_regime': reg,
        }
        rows.append(row)
        print(
            f"{m!s:<8} {bm_ret:>+6.1f}% {s85['return_pct']:>+9.1f}% {s85['mdd_pct']:>+7.1f}% "
            f"{s9['return_pct']:>+9.1f}% {s9['mdd_pct']:>+7.1f}% {s9['trades']:>6} "
            f"{(s9['win_rate_pct'] or 0):>6.1f}% {(s9['avg_trade_pct'] or 0):>+7.1f}%"
        )

    # H1 累計
    h1_85 = eq85s.iloc[-1] / eq85s.iloc[0] - 1
    h1_9 = eq9s.iloc[-1] / eq9s.iloc[0] - 1
    h1_dd85 = (eq85s / eq85s.cummax() - 1).min()
    h1_dd9 = (eq9s / eq9s.cummax() - 1).min()
    bm_h1 = bm.loc['2025-01':'2025-06']
    bench_total = (1 + bm_h1 / 100).prod() - 1 if len(bm_h1) else 0

    print('-' * 100)
    print(
        f"{'H1累計':<8} {bench_total*100:>+6.1f}% {h1_85*100:>+9.1f}% {h1_dd85*100:>+7.1f}% "
        f"{h1_9*100:>+9.1f}% {h1_dd9*100:>+7.1f}%"
    )

    print('\n📊 v9 每月波動 regime（關稅期風控狀態）')
    for r in rows:
        reg = r.get('v9_regime', {})
        if not reg:
            continue
        days = reg.get('regime_days', {})
        day_str = ', '.join(f"{k}:{v}d" for k, v in sorted(days.items()))
        print(
            f"  {r['month']}  avg_fvol={reg.get('avg_fvol_pct')}% "
            f"max_fvol={reg.get('max_fvol_pct')}%  [{day_str}]"
        )

    print('\n📋 v9 每月主要出場原因（前3）')
    for r in rows:
        reasons = r.get('v9_top_exit_reasons', {})
        if not reasons:
            continue
        rs = ', '.join(f"{k}({v})" for k, v in reasons.items())
        print(f"  {r['month']}: {rs}")

    out = 'artifacts/monthly_2025h1.csv'
    os.makedirs('artifacts', exist_ok=True)
    flat = []
    for r in rows:
        flat.append({
            'month': r['month'],
            'bench_0050_pct': r['bench_0050_pct'],
            'v85_return_pct': r['v85_return_pct'],
            'v85_mdd_pct': r['v85_mdd_pct'],
            'v9_return_pct': r['v9_return_pct'],
            'v9_mdd_pct': r['v9_mdd_pct'],
            'v9_trades': r['v9_trades'],
            'v9_win_rate_pct': r['v9_win_rate_pct'],
            'v9_vol_ann_pct': r['v9_vol_ann_pct'],
            'v9_avg_fvol_pct': r.get('v9_regime', {}).get('avg_fvol_pct'),
            'v9_max_fvol_pct': r.get('v9_regime', {}).get('max_fvol_pct'),
        })
    pd.DataFrame(flat).to_csv(out, index=False)
    print(f'\n📁 {out}')


if __name__ == '__main__':
    main()