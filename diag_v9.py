#!/usr/bin/env python3
"""診斷 v9：為何近期回撤變大、輸 v8.5；掃描風險旋鈕找更佳風險/報酬組。

重用 sweep_vol_rotation.load_bundle（只下載一次資料），再跑 v8.5 + 多個 v9 變體。
主要旋鈕：
  - stress_core_ceiling：高波動時 Core 可加碼上限（1.5 = 可加槓桿放大；1.0 = 不加碼）
  - target_ann_vol：組合年化波動目標（越低 → 越早縮倉）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from sweep_vol_rotation import load_bundle
from strategy.event_backtest import EventDrivenBacktester
from strategy.risk_metrics import compute_risk_metrics
from strategy.portfolio_vol_target import v3_production_kwargs


import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument('--days', type=int, default=1200)
_ap.add_argument('--start-date', dest='start_date', default=None)
_args, _ = _ap.parse_known_args()


class A:
    days = _args.days
    start_date = _args.start_date
    end_date = eval_start = None
    top_k = 7
    hold_days = 20
    tp_atr = 4.0
    sl_atr = 3.0
    gap_filter = 1.5
    regime_floor = 0.10
    capital = 200_000
    position_size = 0.10
    universe_size = 60


def run(bundle, hybrid, scc=1.50, tav=0.15, ssf=0.85,
        core_base=0.25, rotation=True, label=''):
    close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, total_score, ma_60 = bundle
    kwargs = dict(
        tp_sl_mode='atr', tp_atr_mult=A.tp_atr, sl_atr_mult=A.sl_atr,
        max_hold_days=A.hold_days, initial_capital=A.capital, position_size=A.position_size,
        regime_filter=True, regime_graduated=True, regime_floor=A.regime_floor,
        gap_filter_atr=A.gap_filter, breadth_regime=True,
        hybrid_tiered=hybrid, buy_cost=0.001425, sell_cost=0.004425,
    )
    if hybrid:
        kw = v3_production_kwargs()
        kw['stress_core_ceiling'] = scc
        kw['stress_sat_floor'] = ssf
        if not rotation:
            # 關閉 alpha-trim + 冷卻輪動
            kw['core_alpha_trim_frac'] = 0.0
            kw['sat_alpha_trim_frac'] = 0.0
            kw['cooling_sat_boost'] = 1.0
            kw['cooling_core_boost'] = 1.0
        kwargs.update(
            core_tickers=['2330', '2454', '2308', '2317', '3008'],
            target_ann_vol=tav, core_base_exposure=core_base,
            corr_filter=0.0, gap_aware_sizing=False, slippage=0.0,
            **kw,
        )
    bt = EventDrivenBacktester(**kwargs)
    trades_df, equity_df = bt.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=A.top_k, threshold=2.0, market_close=market_close,
        vol_df=vol_df, universe_mask=universe_mask,
    )
    m = compute_risk_metrics(equity_df, trades_df, A.capital)
    # 近 60 交易日報酬（觀察近期）
    eq = equity_df['Equity']
    recent = (eq.iloc[-1] / eq.iloc[-60] - 1) * 100 if len(eq) > 60 else float('nan')
    return {
        'label': label, 'ann': m.get('annual_return', m.get('ann_return', 0)) * 100,
        'sharpe': m.get('sharpe', 0), 'mdd': m.get('max_drawdown_pct', 0) * 100,
        'calmar': m.get('calmar', 0), 'trades': len(trades_df), 'recent60': recent,
    }


def main():
    bundle = load_bundle(A)
    rows = []
    win = A.start_date or f'近 {A.days} 日'
    print(f"\n[視窗 {win}]")
    rows.append(run(bundle, False, label='v8.5 (baseline)'))
    rows.append(run(bundle, True, label='v9 V3 (現行,含輪動)'))
    rows.append(run(bundle, True, rotation=False, label='v9 無輪動'))
    rows.append(run(bundle, True, rotation=False, tav=0.12, label='v9 無輪動+tav12'))
    rows.append(run(bundle, True, rotation=False, tav=0.10, ssf=0.70,
                    label='v9 無輪動+tav10+ssf0.7'))

    print('\n' + '=' * 92)
    print(f"{'策略':<26}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}{'近60日%':>10}{'交易':>7}")
    print('-' * 92)
    for r in rows:
        print(f"{r['label']:<26}{r['ann']:>9.1f}{r['sharpe']:>9.2f}{r['mdd']:>9.1f}"
              f"{r['calmar']:>9.2f}{r['recent60']:>10.1f}{r['trades']:>7}")
    print('=' * 92)


if __name__ == '__main__':
    main()
