#!/usr/bin/env python3
"""v9 優化：(A) 分年 walk-forward OOS  (B) rotation_trigger 掃描。
7 年資料只下載一次，兩部分共用。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from sweep_vol_rotation import load_bundle
from strategy.event_backtest import EventDrivenBacktester
from strategy.risk_metrics import compute_risk_metrics
from strategy.portfolio_vol_target import v3_production_kwargs


class A:
    days = 2600
    start_date = '2019-01-01'
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


def run(bundle, hybrid, trigger=0.22, crisis=0.30, rotation=True,
        scc=1.50, tav=0.15, ssf=0.85, regime_gated=False):
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
        kw['rotation_trigger_vol'] = trigger
        kw['crisis_vol'] = crisis
        kw['stress_core_ceiling'] = scc
        kw['stress_sat_floor'] = ssf
        if not rotation:
            kw['core_alpha_trim_frac'] = 0.0
            kw['sat_alpha_trim_frac'] = 0.0
            kw['cooling_sat_boost'] = 1.0
            kw['cooling_core_boost'] = 1.0
        # 註：regime_gated_rotation 實驗已驗證為負面效果並從引擎還原，故此參數不再傳入。
        kwargs.update(core_tickers=['2330', '2454', '2308', '2317', '3008'],
                      target_ann_vol=tav, corr_filter=0.0, gap_aware_sizing=False,
                      slippage=0.0, **kw)
    bt = EventDrivenBacktester(**kwargs)
    trades_df, equity_df = bt.run(
        total_score, close_df, open_df, high_df, low_df, ma_60,
        top_k=A.top_k, threshold=2.0, market_close=market_close,
        vol_df=vol_df, universe_mask=universe_mask)
    return trades_df, equity_df


def full_metrics(equity_df, trades_df):
    m = compute_risk_metrics(equity_df, trades_df, A.capital)
    return (m.get('annual_return', m.get('ann_return', 0)) * 100,
            m.get('sharpe', 0), m.get('max_drawdown_pct', 0) * 100, m.get('calmar', 0))


def year_metrics(equity_df, year):
    eq = equity_df['Equity']
    sub = eq[eq.index.year == year]
    if len(sub) < 5:
        return None
    ret = (sub.iloc[-1] / sub.iloc[0] - 1) * 100
    dr = sub.pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    peak = sub.cummax()
    mdd = ((sub - peak) / peak).min() * 100
    return ret, sharpe, mdd


def main_step3():
    bundle = load_bundle(A)
    t85, eq85 = run(bundle, False)
    t9, eq9 = run(bundle, True)                       # v9 V3
    tg, eqg = run(bundle, True, regime_gated=True)    # v9 gated (Step 3)

    print('\n' + '=' * 70)
    print('【C. Step3 regime 條件式放行】 7年全週期')
    print('=' * 70)
    print(f"{'config':<22}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}")
    print('-' * 70)
    for lab, eq, tr in [('v8.5', eq85, t85), ('v9 V3', eq9, t9), ('v9 gated', eqg, tg)]:
        a = full_metrics(eq, tr)
        print(f"{lab:<22}{a[0]:>9.1f}{a[1]:>9.2f}{a[2]:>9.1f}{a[3]:>9.2f}")
    print('-' * 70)
    print('分年報酬%(看 gated 是否補回 2023/2026 爆多年，且不傷其他年):')
    print(f"{'年':<6}{'v8.5':>9}{'v9 V3':>9}{'v9 gated':>10}")
    for y in sorted(set(eq85.index.year)):
        a = year_metrics(eq85, y); b = year_metrics(eq9, y); c = year_metrics(eqg, y)
        if not (a and b and c):
            continue
        print(f"{y:<6}{a[0]:>9.1f}{b[0]:>9.1f}{c[0]:>10.1f}")
    print('=' * 70)


def metrics_from_equity(eq):
    """從淨值 Series 算 (年化%, Sharpe, MDD%, Calmar)。"""
    dr = eq.pct_change().dropna()
    n = len(eq)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    years = n / 252
    ann = ((1 + total) ** (1 / years) - 1) if years > 0 else 0
    vol = dr.std() * np.sqrt(252)
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    peak = eq.cummax()
    mdd = ((eq - peak) / peak).min()
    calmar = ann / abs(mdd) if mdd < 0 else 0
    return ann * 100, sharpe, mdd * 100, calmar


def main_switch():
    """理想化 regime-switching 可行性篩選：平時 v8.5、risk-off 切 v9。
    (上界估計：忽略換倉延遲/成本，若此版本贏不過 v9 就不值得做工程。)"""
    bundle = load_bundle(A)
    t85, eq85 = run(bundle, False)
    t9, eq9 = run(bundle, True)
    mc = bundle[6]  # market_close (0050)
    idx = eq85.index
    r85 = eq85['Equity'].pct_change()
    r9 = eq9['Equity'].pct_change()
    ma60 = mc.rolling(60).mean().reindex(idx, method='ffill')
    ma20 = mc.rolling(20).mean().reindex(idx, method='ffill')
    mca = mc.reindex(idx, method='ffill')

    # 因果(t-1)regime 訊號 → 當日要走哪個策略
    calmA = (mca > ma60).shift(1).fillna(True)                       # 0050 > MA60 → v8.5
    calmB = ((mca > ma60) & (ma20 > ma60)).shift(1).fillna(True)     # 對齊上升趨勢 → v8.5

    def switched(calm):
        r = r85.where(calm, r9).fillna(0)
        return (1 + r).cumprod() * A.capital

    print('\n' + '=' * 70)
    print('【regime-switching 可行性篩選】 7年 (平時v8.5 / risk-off切v9)')
    print('=' * 70)
    print(f"{'策略':<28}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}")
    print('-' * 70)
    for lab, eq in [('v8.5', eq85['Equity']), ('v9 V3', eq9['Equity']),
                    ('switch A (0050>MA60)', switched(calmA)),
                    ('switch B (對齊上升)', switched(calmB))]:
        m = metrics_from_equity(eq)
        print(f"{lab:<28}{m[0]:>9.1f}{m[1]:>9.2f}{m[2]:>9.1f}{m[3]:>9.2f}")
    print('-' * 70)
    print('註: switch 為理想化上界(無換倉延遲/成本)。分年走哪個(calmA)占比:')
    for y in sorted(set(idx.year)):
        sub = calmA[idx.year == y]
        if len(sub):
            print(f"  {y}: v8.5天數占 {sub.mean()*100:.0f}%")
    print('=' * 70)


def main():
    bundle = load_bundle(A)

    # ---------- Part A: 分年 walk-forward OOS (v8.5 vs v9 V3) ----------
    t85, eq85 = run(bundle, False)
    t9, eq9 = run(bundle, True)  # v9 V3
    years = sorted(set(eq85.index.year))
    print('\n' + '=' * 84)
    print('【A. 分年 OOS】 v8.5 vs v9 V3（固定參數，逐年表現）')
    print('=' * 84)
    print(f"{'年':<6}{'v8.5報酬%':>11}{'v8.5MDD%':>10}{'v8.5Shp':>9}   "
          f"{'v9報酬%':>10}{'v9MDD%':>9}{'v9Shp':>8}  {'v9勝?':>6}")
    print('-' * 84)
    v9_wins = 0
    for y in years:
        a = year_metrics(eq85, y)
        b = year_metrics(eq9, y)
        if not a or not b:
            continue
        # 風險調整勝出：Sharpe 較高 或 (報酬相近且 MDD 較小)
        win = b[1] > a[1]
        v9_wins += int(win)
        print(f"{y:<6}{a[0]:>11.1f}{a[2]:>10.1f}{a[1]:>9.2f}   "
              f"{b[0]:>10.1f}{b[2]:>9.1f}{b[1]:>8.2f}  {'✔' if win else '':>6}")
    print('-' * 84)
    print(f"v9 Sharpe 勝出年數: {v9_wins}/{len([y for y in years if year_metrics(eq85,y) and year_metrics(eq9,y)])}")

    # ---------- Part B: rotation_trigger 掃描 (7年全週期) ----------
    print('\n' + '=' * 84)
    print('【B. rotation_trigger 掃描】 7年全週期（找多頭少賺最小、危機保護不丟）')
    print('=' * 84)
    print(f"{'config':<26}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}")
    print('-' * 84)
    a85 = full_metrics(eq85, t85)
    print(f"{'v8.5 (ref)':<26}{a85[0]:>9.1f}{a85[1]:>9.2f}{a85[2]:>9.1f}{a85[3]:>9.2f}")
    for trig in [0.22, 0.28, 0.35]:
        td, ed = run(bundle, True, trigger=trig)
        mm = full_metrics(ed, td)
        print(f"{'v9 trigger='+str(int(trig*100))+'%':<26}{mm[0]:>9.1f}{mm[1]:>9.2f}{mm[2]:>9.1f}{mm[3]:>9.2f}")
    print('=' * 84)


if __name__ == '__main__':
    if '--switch' in sys.argv:
        main_switch()
    elif '--step3' in sys.argv:
        main_step3()
    else:
        main()
