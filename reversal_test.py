#!/usr/bin/env python3
"""均值回歸(反轉)sleeve 研究 — 找與動能(v9)低/負相關的 alpha,混合拉高組合 Sharpe。
嚴格驗證:相關性 + 全週期 + 近期 + 混合是否真的同時提升報酬與降風險。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from sweep_vol_rotation import load_bundle
from strategy.event_backtest import EventDrivenBacktester
from strategy.portfolio_vol_target import v3_production_kwargs
from twstk.portfolio import PortfolioConfig, simulate_weights, equity_dataframe


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


def run_v9(bundle):
    close_df, open_df, high_df, low_df, vol_df, um, mc, score, ma_60 = bundle
    bt = EventDrivenBacktester(
        tp_sl_mode='atr', tp_atr_mult=A.tp_atr, sl_atr_mult=A.sl_atr, max_hold_days=A.hold_days,
        initial_capital=A.capital, position_size=A.position_size, regime_filter=True,
        regime_graduated=True, regime_floor=A.regime_floor, gap_filter_atr=A.gap_filter,
        breadth_regime=True, hybrid_tiered=True, core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15, buy_cost=0.001425, sell_cost=0.004425,
        corr_filter=0.0, gap_aware_sizing=False, slippage=0.0, **v3_production_kwargs())
    _, ed = bt.run(score, close_df, open_df, high_df, low_df, ma_60, top_k=A.top_k,
                   threshold=2.0, market_close=mc, vol_df=vol_df, universe_mask=um)
    return ed['Equity']


def reversal_weights(close, um, lookback, k, above_ma=None, hold=5):
    """買「近 lookback 日跌最多」的 k 檔(超跌反轉),等權。above_ma:只在 close>MAn 內選。"""
    ret = close.pct_change(lookback)
    elig = um & close.notna() & (close > 0)
    if above_ma is not None:
        elig = elig & (close > close.rolling(above_ma).mean())
    masked = ret.where(elig)
    rank = masked.rank(axis=1, ascending=True)  # 跌最多 = rank 小
    sel = (rank <= k) & masked.notna()
    # 降週轉:每 hold 天才換一次(持有期)
    w = sel.astype(float)
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    if hold > 1:
        w = w.iloc[::hold].reindex(w.index, method='ffill').fillna(0.0)
    return w


def run_reversal(bundle, lookback, k, above_ma=None, hold=5):
    close_df, open_df = bundle[0], bundle[1]
    um = bundle[5].fillna(False)
    w = reversal_weights(close_df, um, lookback, k, above_ma, hold)
    pcfg = PortfolioConfig(initial_capital=A.capital, buy_cost=0.001425, sell_cost=0.004425)
    st = simulate_weights(w, open_df, close_df, pcfg,
                          start=close_df.index[60].strftime('%Y-%m-%d'))
    return equity_dataframe(st)['Equity']


def blend(curves, weights):
    rets = pd.DataFrame({k: c.pct_change() for k, c in curves.items()}).dropna()
    wv = np.array([weights[k] for k in rets.columns]); wv = wv / wv.sum()
    return (1 + (rets * wv).sum(axis=1)).cumprod() * A.capital


def report(title, curves, window=None):
    print('\n' + '=' * 64)
    print(title)
    print('=' * 64)
    if window:
        curves = {k: v[v.index >= pd.Timestamp(window)] for k, v in curves.items()}
    print(f"{'策略/組合':<22}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}")
    for k, c in curves.items():
        m = metrics(c)
        print(f"{k:<22}{m[0]:>9.1f}{m[1]:>9.2f}{m[2]:>9.1f}{m[3]:>9.2f}")


def main():
    bundle = load_bundle(A)
    eq9 = run_v9(bundle)
    rev5 = run_reversal(bundle, lookback=5, k=7, hold=5)
    rev20 = run_reversal(bundle, lookback=20, k=7, hold=10)
    rev5q = run_reversal(bundle, lookback=5, k=7, above_ma=60, hold=5)  # 上升趨勢內超跌

    # 對齊到 v9 的索引
    idx = eq9.index
    rev5 = rev5.reindex(idx).ffill(); rev20 = rev20.reindex(idx).ffill(); rev5q = rev5q.reindex(idx).ffill()
    base = {'v9 V3': eq9, '反轉5d': rev5, '反轉20d': rev20, '反轉5d+趨勢': rev5q}

    rets = pd.DataFrame({k: c.pct_change() for k, c in base.items()}).dropna()
    print('\n=== 日報酬相關性(對 v9 越低/負,分散越好)===')
    print(rets.corr().round(2).to_string())

    combos = {'v9 V3': eq9}
    for name, rev in [('反轉5d', rev5), ('反轉20d', rev20), ('反轉5d+趨勢', rev5q)]:
        combos[f'v9 80% +{name}20%'] = blend({'v9': eq9, 'r': rev}, {'v9': 0.8, 'r': 0.2})
        combos[f'v9 70% +{name}30%'] = blend({'v9': eq9, 'r': rev}, {'v9': 0.7, 'r': 0.3})

    report('【全週期 2019–2026】 v9 vs v9+反轉混合', combos)
    report('【近期 2024-06 起】 v9 vs v9+反轉混合', combos, window='2024-06-01')


if __name__ == '__main__':
    main()
