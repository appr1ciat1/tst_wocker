#!/usr/bin/env python3
"""SBL(借券賣出)因子 — 全週期(2019+)驗證。上市股有深歷史,故可做全週期 + 分年 IC。
檢驗:① 分年 IC 是否穩定為負(法人放空有效)② 權重掃描是否平滑(非過擬合)③ 分年回測。
"""
import sys, os, time, json, ssl, pickle, urllib.request, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from sweep_vol_rotation import load_bundle
from strategy.event_backtest import EventDrivenBacktester
from strategy.risk_metrics import compute_risk_metrics
from strategy.portfolio_vol_target import v3_production_kwargs

_CTX = ssl.create_default_context(); _CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT
CACHE = 'artifacts/sbl_7yr_cache.pkl'
START = '2019-01-01'; END = '2026-06-17'


class A:
    days = 2600; start_date = START; end_date = eval_start = None
    top_k = 7; hold_days = 20; tp_atr = 4.0; sl_atr = 3.0; gap_filter = 1.5
    regime_floor = 0.10; capital = 200_000; position_size = 0.10; universe_size = 60


def _fetch(ticker):
    p = {'dataset': 'TaiwanDailyShortSaleBalances', 'data_id': ticker,
         'start_date': START, 'end_date': END}
    url = 'https://api.finmindtrade.com/api/v4/data?' + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        d = json.loads(urllib.request.urlopen(req, context=_CTX, timeout=30).read().decode())
        return d.get('data', []) if d.get('status') == 200 else []
    except Exception:
        return []


def build_sbl(tickers, idx):
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            sbl = pickle.load(f)
        print(f"   ✅ SBL 7yr 快取: {sbl.shape}")
        return sbl.reindex(index=idx, columns=tickers)
    print(f"📥 抓 {len(tickers)} 檔 SBL 全歷史 ...")
    d = {}; ok = 0; deep = 0
    for i, t in enumerate(tickers):
        rows = _fetch(t)
        if rows:
            ok += 1
            if pd.Timestamp(rows[0]['date']) < pd.Timestamp('2024-01-01'):
                deep += 1
            for r in rows:
                d.setdefault(pd.Timestamp(r['date']), {})[t] = r.get('SBLShortSalesCurrentDayBalance', np.nan)
        if (i + 1) % 25 == 0:
            print(f"   {i+1}/{len(tickers)} (ok={ok}, 深歷史={deep})")
        time.sleep(0.25)
    sbl = pd.DataFrame.from_dict(d, orient='index').sort_index()
    os.makedirs('artifacts', exist_ok=True)
    with open(CACHE, 'wb') as f:
        pickle.dump(sbl, f)
    print(f"   ✅ ok={ok}, 深歷史(<2024起)={deep}, 快取 {CACHE}")
    return sbl.reindex(index=idx, columns=tickers)


def spearman_ic(factor, fwd, um, year=None):
    ics = []
    for dt in factor.index:
        if year and dt.year != year:
            continue
        f = factor.loc[dt].where(um.loc[dt]) if dt in um.index else factor.loc[dt]
        r = fwd.loc[dt] if dt in fwd.index else None
        if r is None:
            continue
        df = pd.DataFrame({'f': f, 'r': r}).dropna()
        if len(df) >= 8:
            ics.append(df['f'].rank().corr(df['r'].rank()))
    ics = [x for x in ics if pd.notna(x)]
    return (np.mean(ics), len(ics)) if ics else (np.nan, 0)


def run_v9(bundle, score):
    close_df, open_df, high_df, low_df, vol_df, um, mc, _, ma_60 = bundle
    bt = EventDrivenBacktester(
        tp_sl_mode='atr', tp_atr_mult=A.tp_atr, sl_atr_mult=A.sl_atr, max_hold_days=A.hold_days,
        initial_capital=A.capital, position_size=A.position_size, regime_filter=True,
        regime_graduated=True, regime_floor=A.regime_floor, gap_filter_atr=A.gap_filter,
        breadth_regime=True, hybrid_tiered=True, core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15, buy_cost=0.001425, sell_cost=0.004425,
        corr_filter=0.0, gap_aware_sizing=False, slippage=0.0, **v3_production_kwargs())
    td, ed = bt.run(score, close_df, open_df, high_df, low_df, ma_60, top_k=A.top_k,
                    threshold=2.0, market_close=mc, vol_df=vol_df, universe_mask=um)
    m = compute_risk_metrics(ed, td, A.capital)
    return (m.get('annual_return', m.get('ann_return', 0)) * 100, m.get('sharpe', 0),
            m.get('max_drawdown_pct', 0) * 100, m.get('calmar', 0), len(td)), ed


def main():
    bundle = load_bundle(A)
    close_df = bundle[0]; um = bundle[5].fillna(False); base = bundle[7]
    sbl = build_sbl(list(close_df.columns), close_df.index)
    cover = sbl.notna().any()
    print(f"   有 SBL 資料的標的: {int(cover.sum())}/{len(close_df.columns)}")

    chg = sbl.pct_change(20)
    rk = lambda df: df.where(um).rank(axis=1, pct=True)
    fwd20 = close_df.shift(-20) / close_df - 1
    f_sbl = (-rk(chg)).shift(1).reindex_like(base).fillna(0.0)

    print('\n' + '=' * 60)
    print('【分年 IC】 借券賣出20日變化 vs 20日前瞻報酬 (IC<0 = 有效)')
    print('=' * 60)
    print(f"{'年':<8}{'IC':>10}{'樣本日':>8}")
    for y in sorted(set(close_df.index.year)):
        ic, n = spearman_ic(chg, fwd20, um, year=y)
        if n:
            print(f"{y:<8}{ic:>+10.4f}{n:>8}")
    icall, nall = spearman_ic(chg, fwd20, um)
    print(f"{'全期':<8}{icall:>+10.4f}{nall:>8}")

    print('\n' + '=' * 70)
    print('【權重掃描 + 分年回測】 v9 + SBL(借券) (7年全週期)')
    print('=' * 70)
    print(f"{'config':<16}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}{'交易':>7}")
    eds = {}
    for w in [0.0, 0.25, 0.5, 0.75]:
        r, ed = run_v9(bundle, base + w * f_sbl)
        eds[w] = ed
        print(f"{'w='+str(w):<16}{r[0]:>9.1f}{r[1]:>9.2f}{r[2]:>9.1f}{r[3]:>9.2f}{r[4]:>7}")
    print('-' * 70)
    print('分年報酬%: base vs +SBL0.25')
    yr = lambda ed, y: (lambda s: (s.iloc[-1]/s.iloc[0]-1)*100 if len(s) > 5 else None)(
        ed['Equity'][ed['Equity'].index.year == y])
    for y in sorted(set(close_df.index.year)):
        a = yr(eds[0.0], y); b = yr(eds[0.25], y)
        if a is not None and b is not None:
            print(f"  {y}: {a:>7.1f}  →  {b:>7.1f}  ({b-a:>+6.1f})")
    print('=' * 70)


if __name__ == '__main__':
    main()
