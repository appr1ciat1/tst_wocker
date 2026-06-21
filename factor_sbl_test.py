#!/usr/bin/env python3
"""借券賣出(SBL)因子探索 — 法人空方訊號(非散戶融券)。資料僅~1年(2025-06+)。
短資料 → 用 IC(資訊係數)當主要訊號品質指標,1年回測當輔助,並強調過擬合風險。
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
CACHE = 'artifacts/sbl_cache.pkl'
START = '2025-06-01'


class A:
    days = 400
    start_date = START
    end_date = eval_start = None
    top_k = 7; hold_days = 20; tp_atr = 4.0; sl_atr = 3.0; gap_filter = 1.5
    regime_floor = 0.10; capital = 200_000; position_size = 0.10; universe_size = 60


def fetch_sbl_one(ticker):
    p = {'dataset': 'TaiwanDailyShortSaleBalances', 'data_id': ticker,
         'start_date': START, 'end_date': '2026-06-17'}
    url = 'https://api.finmindtrade.com/api/v4/data?' + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    d = json.loads(urllib.request.urlopen(req, context=_CTX, timeout=30).read().decode('utf-8'))
    return d.get('data', []) if d.get('status') == 200 else []


def build_sbl(tickers, idx):
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            sbl = pickle.load(f)
        print(f"   ✅ SBL 快取: {sbl.shape}")
        return sbl.reindex(index=idx, columns=tickers)
    print(f"📥 FinMind 抓 {len(tickers)} 檔借券賣出餘額 ...")
    d = {}
    ok = 0
    for i, t in enumerate(tickers):
        try:
            rows = fetch_sbl_one(t)
        except Exception:
            rows = []
        if rows:
            ok += 1
            for r in rows:
                dt = pd.Timestamp(r['date'])
                d.setdefault(dt, {})[t] = r.get('SBLShortSalesCurrentDayBalance', np.nan)
        if (i + 1) % 20 == 0:
            print(f"   {i+1}/{len(tickers)} (ok={ok})")
        time.sleep(0.25)
    sbl = pd.DataFrame.from_dict(d, orient='index').sort_index()
    os.makedirs('artifacts', exist_ok=True)
    with open(CACHE, 'wb') as f:
        pickle.dump(sbl, f)
    print(f"   ✅ ok={ok}, 快取 {CACHE}")
    return sbl.reindex(index=idx, columns=tickers)


def spearman_ic(factor, fwd, um):
    """逐日橫斷面 spearman(factor, fwd_ret) 的平均 IC。"""
    ics = []
    for dt in factor.index:
        f = factor.loc[dt].where(um.loc[dt] if dt in um.index else None)
        r = fwd.loc[dt] if dt in fwd.index else None
        if r is None:
            continue
        df = pd.DataFrame({'f': f, 'r': r}).dropna()
        if len(df) >= 8:
            ics.append(df['f'].rank().corr(df['r'].rank()))
    ics = [x for x in ics if pd.notna(x)]
    return (np.mean(ics), np.std(ics), len(ics)) if ics else (np.nan, np.nan, 0)


def run_v9(bundle, score):
    close_df, open_df, high_df, low_df, vol_df, universe_mask, market_close, _, ma_60 = bundle
    kw = v3_production_kwargs()
    bt = EventDrivenBacktester(
        tp_sl_mode='atr', tp_atr_mult=A.tp_atr, sl_atr_mult=A.sl_atr, max_hold_days=A.hold_days,
        initial_capital=A.capital, position_size=A.position_size, regime_filter=True,
        regime_graduated=True, regime_floor=A.regime_floor, gap_filter_atr=A.gap_filter,
        breadth_regime=True, hybrid_tiered=True, core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15, buy_cost=0.001425, sell_cost=0.004425,
        corr_filter=0.0, gap_aware_sizing=False, slippage=0.0, **kw)
    td, ed = bt.run(score, close_df, open_df, high_df, low_df, ma_60,
                    top_k=A.top_k, threshold=2.0, market_close=market_close,
                    vol_df=vol_df, universe_mask=universe_mask)
    m = compute_risk_metrics(ed, td, A.capital)
    return (m.get('annual_return', m.get('ann_return', 0)) * 100, m.get('sharpe', 0),
            m.get('max_drawdown_pct', 0) * 100, m.get('calmar', 0), len(td))


def main():
    bundle = load_bundle(A)
    close_df = bundle[0]; um = bundle[5].fillna(False); base = bundle[7]
    sbl = build_sbl(list(close_df.columns), close_df.index)

    # 借券賣出餘額(法人空方)。變化方向用 IC 判定。
    sbl_chg = sbl.pct_change(20)
    sbl_lvl = sbl / (close_df * 0 + 1)  # 餘額水準(規模未標準化,僅供 rank)
    rk = lambda df: df.where(um).rank(axis=1, pct=True)

    print('\n' + '=' * 70)
    print('【借券(SBL)因子 IC 分析】 ~1年, 20日前瞻報酬')
    print('=' * 70)
    fwd20 = close_df.shift(-20) / close_df - 1
    for name, fac in [('SBL餘額20日變化', sbl_chg), ('SBL餘額水準', sbl_lvl),
                      ('SBL餘額(原始rank)', rk(sbl))]:
        ic, sd, n = spearman_ic(fac, fwd20, um)
        ir = ic / sd * np.sqrt(n) if sd and n else float('nan')
        print(f"  {name:<22} IC={ic:+.4f}  IC_std={sd:.4f}  IR={ir:+.2f}  n={n}")
    print('  (IC<0 代表「借券賣出越多→未來報酬越低」=法人看空有效;|IC|>0.03 才算有訊號)')

    # 依 IC 方向建因子：rising SBL short = 看空 → 取負 rank
    f_sbl = (-rk(sbl_chg)).shift(1).reindex_like(base).fillna(0.0)

    print('\n' + '=' * 70)
    print('【1年回測(輔助,過擬合風險高)】 v9 + 借券因子')
    print('=' * 70)
    print(f"{'config':<26}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}{'交易':>7}")
    print('-' * 70)
    for lab, w in [('v9 base', 0.0), ('v9 +SBL 0.25', 0.25), ('v9 +SBL 0.5', 0.5), ('v9 +SBL 0.75', 0.75)]:
        r = run_v9(bundle, base + w * f_sbl)
        print(f"{lab:<26}{r[0]:>9.1f}{r[1]:>9.2f}{r[2]:>9.1f}{r[3]:>9.2f}{r[4]:>7}")
    print('=' * 70)


if __name__ == '__main__':
    main()
