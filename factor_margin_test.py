#!/usr/bin/env python3
"""新因子實驗：融資融券(FinMind) 加進 v9 選股評分，看是否提升風險調整報酬。

因子(皆橫向 rank,加到 momentum total_score):
  - margin_mom: 融資餘額 20 日變化（contrarian：融資縮 = 籌碼穩 = 看多 → 取負）
  - short_ratio: 券資比(融券/融資 餘額) 升高（軋空燃料 → 看多）
資料只下載一次(7yr)，margin 快取到 artifacts/margin_cache.pkl。
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
CACHE = 'artifacts/margin_cache.pkl'


class A:
    days = 2600
    start_date = '2019-01-01'
    end_date = eval_start = None
    top_k = 7; hold_days = 20; tp_atr = 4.0; sl_atr = 3.0; gap_filter = 1.5
    regime_floor = 0.10; capital = 200_000; position_size = 0.10; universe_size = 60


def fetch_margin_one(ticker, start, end):
    p = {'dataset': 'TaiwanStockMarginPurchaseShortSale', 'data_id': ticker,
         'start_date': start, 'end_date': end}
    url = 'https://api.finmindtrade.com/api/v4/data?' + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    d = json.loads(urllib.request.urlopen(req, context=_CTX, timeout=30).read().decode('utf-8'))
    return d.get('data', []) if d.get('status') == 200 else []


def build_margin(tickers, idx, start, end):
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            mb, sb = pickle.load(f)
        print(f"   ✅ margin 快取載入: {mb.shape}")
        return mb.reindex(index=idx, columns=tickers), sb.reindex(index=idx, columns=tickers)
    print(f"📥 FinMind 抓 {len(tickers)} 檔融資融券 ...")
    mb_d, sb_d = {}, {}
    ok = 0
    for i, t in enumerate(tickers):
        try:
            rows = fetch_margin_one(t, start, end)
        except Exception:
            rows = []
        if rows:
            ok += 1
            for r in rows:
                dt = pd.Timestamp(r['date'])
                mb_d.setdefault(dt, {})[t] = r.get('MarginPurchaseTodayBalance', np.nan)
                sb_d.setdefault(dt, {})[t] = r.get('ShortSaleTodayBalance', np.nan)
        if (i + 1) % 20 == 0:
            print(f"   {i+1}/{len(tickers)} (ok={ok})")
        time.sleep(0.25)  # 尊重 FinMind 速率
    mb = pd.DataFrame.from_dict(mb_d, orient='index').sort_index()
    sb = pd.DataFrame.from_dict(sb_d, orient='index').sort_index()
    os.makedirs('artifacts', exist_ok=True)
    with open(CACHE, 'wb') as f:
        pickle.dump((mb, sb), f)
    print(f"   ✅ 抓完 ok={ok}，快取 {CACHE}")
    return mb.reindex(index=idx, columns=tickers), sb.reindex(index=idx, columns=tickers)


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
            m.get('max_drawdown_pct', 0) * 100, m.get('calmar', 0), len(td)), ed


def _year_ret(ed, y):
    eq = ed['Equity']; sub = eq[eq.index.year == y]
    return (sub.iloc[-1] / sub.iloc[0] - 1) * 100 if len(sub) > 5 else None


def main():
    bundle = load_bundle(A)
    close_df = bundle[0]; universe_mask = bundle[5]; base_score = bundle[7]
    mb, sb = build_margin(list(close_df.columns), close_df.index, A.start_date, '2026-06-17')

    # 因子(用 t-1 避免前視)
    um = universe_mask.fillna(False)
    margin_mom = mb.pct_change(20)
    short_ratio = (sb / mb.replace(0, np.nan))
    def rank_in_univ(df):
        return df.where(um).rank(axis=1, pct=True)
    f_margin = (-rank_in_univ(margin_mom)).shift(1)      # 融資縮→高分(contrarian)
    f_short = (rank_in_univ(short_ratio)).shift(1)        # 券資比高→高分
    f_margin = f_margin.reindex_like(base_score).fillna(0.0)
    f_short = f_short.reindex_like(base_score).fillna(0.0)

    print('\n' + '=' * 74)
    print('【穩健性 A. 融資 contrarian 權重掃描】 7年')
    print('=' * 74)
    print(f"{'weight':<10}{'年化%':>9}{'Sharpe':>9}{'MDD%':>9}{'Calmar':>9}{'交易':>8}")
    print('-' * 74)
    eds = {}
    for w in [0.0, 0.25, 0.4, 0.5, 0.6, 0.75]:
        r, ed = run_v9(bundle, base_score + w * f_margin)
        eds[w] = ed
        print(f"{w:<10}{r[0]:>9.1f}{r[1]:>9.2f}{r[2]:>9.1f}{r[3]:>9.2f}{r[4]:>8}")
    print('-' * 74)
    print('【穩健性 B. 分年報酬%】 baseline(w=0) vs +margin(w=0.5)')
    print(f"{'年':<6}{'v9 base':>10}{'v9+margin0.5':>14}{'差':>8}")
    for y in sorted(set(base_score.index.year)):
        a = _year_ret(eds[0.0], y); b = _year_ret(eds[0.5], y)
        if a is None or b is None:
            continue
        print(f"{y:<6}{a:>10.1f}{b:>14.1f}{b-a:>+8.1f}")
    print('=' * 74)


if __name__ == '__main__':
    main()
