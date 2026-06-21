#!/usr/bin/env python3
"""多策略分散(投組理論)實測：v8.5 / SR v2 / v9 相關性 + 資金配置混合。
目標:找出在「全週期 + 近期」都報酬更高、風險更小、勝過 v8.5 的組合。
混合 = 拆資金成子帳戶各跑一支(daily-rebalanced),為真實可實作做法。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from sweep_vol_rotation import load_bundle
from strategy.event_backtest import EventDrivenBacktester
from strategy.sector_rotation_backtest import SectorRotationBacktester
from strategy.us_market import fetch_us_signals, align_us_to_tw
from strategy.portfolio_vol_target import v3_production_kwargs


class A:
    days = 2600; start_date = '2019-01-01'; end_date = None; eval_start = None
    top_k = 7; hold_days = 20; tp_atr = 4.0; sl_atr = 3.0; gap_filter = 1.5
    regime_floor = 0.10; capital = 200_000; position_size = 0.10; universe_size = 60


def metrics(eq):
    dr = eq.pct_change().dropna()
    n = len(eq); total = eq.iloc[-1] / eq.iloc[0] - 1; yrs = n / 252
    ann = ((1 + total) ** (1 / yrs) - 1) if yrs > 0 else 0
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() else 0
    mdd = ((eq - eq.cummax()) / eq.cummax()).min()
    return ann * 100, sharpe, mdd * 100, (ann / abs(mdd) if mdd < 0 else 0)


def run_momentum(bundle, hybrid):
    close_df, open_df, high_df, low_df, vol_df, um, mc, score, ma_60 = bundle
    kw = dict(tp_sl_mode='atr', tp_atr_mult=A.tp_atr, sl_atr_mult=A.sl_atr, max_hold_days=A.hold_days,
              initial_capital=A.capital, position_size=A.position_size, regime_filter=True,
              regime_graduated=True, regime_floor=A.regime_floor, gap_filter_atr=A.gap_filter,
              breadth_regime=True, hybrid_tiered=hybrid, buy_cost=0.001425, sell_cost=0.004425)
    if hybrid:
        kw.update(core_tickers=['2330', '2454', '2308', '2317', '3008'], target_ann_vol=0.15,
                  corr_filter=0.0, gap_aware_sizing=False, slippage=0.0, **v3_production_kwargs())
    bt = EventDrivenBacktester(**kw)
    _, ed = bt.run(score, close_df, open_df, high_df, low_df, ma_60, top_k=A.top_k,
                   threshold=2.0, market_close=mc, vol_df=vol_df, universe_mask=um)
    return ed['Equity']


def run_sr(bundle):
    close_df, open_df, high_df, low_df, vol_df, um, mc, score, ma_60 = bundle
    us = fetch_us_signals(start_date=A.start_date, end_date=close_df.index[-1].strftime('%Y-%m-%d'))
    usa = align_us_to_tw(us, close_df.index)
    bt = SectorRotationBacktester(initial_capital=A.capital, tp_atr_mult=A.tp_atr,
                                  sl_atr_mult=A.sl_atr, max_hold_days=A.hold_days)
    _, ed = bt.run(close_df, open_df, high_df, low_df, vol_df, usa, um)
    return ed['Equity']


def blend(curves, weights):
    """拆資金成子帳戶、daily-rebalanced。回傳混合淨值(起始=capital)。"""
    rets = pd.DataFrame({k: c.pct_change() for k, c in curves.items()}).dropna()
    w = np.array([weights[k] for k in rets.columns]); w = w / w.sum()
    br = (rets * w).sum(axis=1)
    return (1 + br).cumprod() * A.capital


def report(title, curves, window=None):
    print('\n' + '=' * 68)
    print(title)
    print('=' * 68)
    if window:
        curves = {k: v[v.index >= pd.Timestamp(window)] for k, v in curves.items()}
    print(f"{'策略/組合':<24}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}")
    for k, c in curves.items():
        m = metrics(c)
        print(f"{k:<24}{m[0]:>9.1f}{m[1]:>9.2f}{m[2]:>9.1f}{m[3]:>9.2f}")


def main():
    bundle = load_bundle(A)
    eq85 = run_momentum(bundle, False)
    eq9 = run_momentum(bundle, True)
    eqsr = run_sr(bundle)
    base = {'v8.5': eq85, 'SR v2': eqsr, 'v9 V3': eq9}

    # 相關性(日報酬)
    rets = pd.DataFrame({k: c.pct_change() for k, c in base.items()}).dropna()
    print('\n=== 日報酬相關性(越低分散效益越大)===')
    print(rets.corr().round(2).to_string())

    # 反波動(risk-parity)權重 v8.5 + SR v2
    vol85 = eq85.pct_change().std(); volsr = eqsr.pct_change().std()
    iv = {'v8.5': 1 / vol85, 'SR v2': 1 / volsr}

    combos = dict(base)
    combos['50/50 v85+SR'] = blend({'v8.5': eq85, 'SR v2': eqsr}, {'v8.5': 0.5, 'SR v2': 0.5})
    combos['risk-parity v85+SR'] = blend({'v8.5': eq85, 'SR v2': eqsr}, iv)
    combos['50/50 v9+SR'] = blend({'v9 V3': eq9, 'SR v2': eqsr}, {'v9 V3': 0.5, 'SR v2': 0.5})
    combos['1/3 v85+SR+v9'] = blend(base, {'v8.5': 1, 'SR v2': 1, 'v9 V3': 1})

    report('【全週期 2019–2026】 單策略 vs 分散組合', combos)
    report('【近期 2024-06 起】 單策略 vs 分散組合', combos, window='2024-06-01')


if __name__ == '__main__':
    main()
