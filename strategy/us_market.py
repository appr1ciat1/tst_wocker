"""
美股宏觀信號模組 (US Market Macro Signals)

下載 S&P 500、VIX、費城半導體指數 (SOX) 的日線數據，
計算趨勢方向、動量、恐慌水位等信號，作為台股板塊輪動策略的前提條件。

核心概念：
- 台股整體板塊跟隨美股大盤 (SPY) + 恐慌指數 (VIX)
- 台股科技電子業前提要看費半 (SOX)
- 美股收盤 = 台灣次日凌晨 → 台股 t 日用美股 t-1 日數據（天然領先）

v1.1 — 2026-04-09  (crisis-tuned thresholds)
"""

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')


def fetch_us_signals(start_date=None, end_date=None, days=1500):
    """
    下載美股三大指標並計算所有技術信號。

    Parameters
    ----------
    start_date : str, optional
        起始日期 (YYYY-MM-DD)
    end_date : str, optional
        結束日期 (YYYY-MM-DD)
    days : int
        回溯天數（start_date 為 None 時使用）

    Returns
    -------
    pd.DataFrame
        包含所有美股信號的 DataFrame，index = trading date
        欄位: spy_close, spy_ma20, spy_ma60, spy_trend, spy_short,
              vix_close, sox_close, sox_ma20, sox_ma60, sox_trend,
              sox_mom_20d, macro_regime, tech_gate
    """
    if end_date:
        end_dt = pd.Timestamp(end_date)
    else:
        end_dt = pd.Timestamp(datetime.today())

    if start_date:
        # 多取 120 天用於 MA 暖機
        start_dt = pd.Timestamp(start_date) - timedelta(days=120)
    else:
        start_dt = end_dt - timedelta(days=days + 120)

    print(f"🌍 下載美股信號 (SPY/VIX/SOX)...")

    # 下載三個指標
    tickers = {'^GSPC': 'spy', '^VIX': 'vix', '^SOX': 'sox'}
    dfs = {}
    for symbol, name in tickers.items():
        data = yf.download(symbol, start=start_dt, end=end_dt, progress=False)
        if data.empty:
            print(f"   ⚠️ {symbol} 下載失敗")
            continue
        close = data['Close'].squeeze()
        close.name = f'{name}_close'
        dfs[name] = close

    if len(dfs) < 3:
        print("   ⚠️ 美股數據不完整，部分指標缺失")

    # 合併成單一 DataFrame
    signals = pd.DataFrame(dfs)
    signals.columns = [f'{name}_close' for name in dfs.keys()]
    signals = signals.ffill()  # 假日填補

    # 計算技術指標
    if 'spy_close' in signals.columns:
        signals['spy_ma20'] = signals['spy_close'].rolling(20).mean()
        signals['spy_ma60'] = signals['spy_close'].rolling(60).mean()
        signals['spy_trend'] = (signals['spy_close'] > signals['spy_ma60']).astype(int)
        signals['spy_short'] = (signals['spy_close'] > signals['spy_ma20']).astype(int)

    if 'vix_close' in signals.columns:
        pass  # VIX 直接用 close 值

    if 'sox_close' in signals.columns:
        signals['sox_ma20'] = signals['sox_close'].rolling(20).mean()
        signals['sox_ma60'] = signals['sox_close'].rolling(60).mean()
        signals['sox_trend'] = (signals['sox_close'] > signals['sox_ma60']).astype(int)
        signals['sox_mom_20d'] = signals['sox_close'] / signals['sox_close'].shift(20) - 1

    # 計算 Macro Regime
    signals['macro_regime'] = _compute_macro_regime_series(signals)

    # 計算 Tech Gate
    signals['tech_gate'] = _compute_tech_gate_series(signals)

    # 裁剪到請求的日期範圍
    if start_date:
        actual_start = pd.Timestamp(start_date)
        signals = signals[signals.index >= actual_start]

    n_days = len(signals)
    first = signals.index[0].strftime('%Y-%m-%d') if n_days > 0 else 'N/A'
    last = signals.index[-1].strftime('%Y-%m-%d') if n_days > 0 else 'N/A'
    print(f"   ✅ 美股信號: {first} → {last} ({n_days} 天)")

    return signals


def _compute_macro_regime_series(signals):
    """
    計算每日的宏觀 regime 曝險比例。

    v1.1 crisis-tuned (基於 11 段歷史危機分析):
    - 弱勢期間 VIX 均值 30.3，門檻從 35 降到 28
    - SPY + SOX 雙空 → 幾乎停止
    - 復甦允許：VIX 25-28 + SPY > MA20 → 半倉

    VIX > 33                            → 0.8 (高恐慌建倉區，不硬停)
    VIX 28~33                           → 0.0 (中等恐慌暫停)
    SPY↓(MA60) + SOX↓(MA60)            → 0.1 (雙空 = 最危險)
    SPY↓ + VIX 25~28 + SPY > MA20      → 0.5 (復甦允許)
    SPY↓ + VIX 25~28                    → 0.2
    SPY↓ + VIX < 25                     → 0.4
    SPY↑ + VIX 22~25                    → 0.7
    SPY↑ + VIX < 22                     → 1.0
    """
    regime = pd.Series(0.4, index=signals.index)

    spy_trend = signals.get('spy_trend', pd.Series(1, index=signals.index))
    spy_short = signals.get('spy_short', pd.Series(1, index=signals.index))
    sox_trend = signals.get('sox_trend', pd.Series(1, index=signals.index))
    vix = signals.get('vix_close', pd.Series(20.0, index=signals.index))

    # ── Layer 1: VIX — >33 建倉區；28~33 暫停 ──
    regime[(vix > 28) & (vix <= 33)] = 0.0
    regime[vix > 33] = 0.8

    # ── Layer 2: SPY + SOX 雙空 → 幾乎停止 ──
    dual_bear = (spy_trend == 0) & (sox_trend == 0) & (vix <= 33)
    regime[dual_bear] = 0.1

    # ── Layer 3: SPY↓ + 中等 VIX ──
    # SPY↓ + VIX 25~28 (中等恐慌)
    mask_down_mid_vix = (spy_trend == 0) & (vix >= 25) & (vix <= 28) & ~dual_bear
    regime[mask_down_mid_vix] = 0.2

    # SPY↓ + VIX 25~28 + SPY > MA20 → 復甦允許 (避免擋住反彈)
    mask_recovery = mask_down_mid_vix & (spy_short == 1)
    regime[mask_recovery] = 0.5

    # SPY↓ + VIX < 25 (溫和空頭)
    mask_down_low_vix = (spy_trend == 0) & (vix < 25) & ~dual_bear
    regime[mask_down_low_vix] = 0.4

    # ── Layer 4: SPY↑ ──
    # SPY↑ + VIX 22~25
    mask_up_mid_vix = (spy_trend == 1) & (vix >= 22) & (vix < 25)
    regime[mask_up_mid_vix] = 0.7

    # SPY↑ + VIX < 22
    mask_up_low_vix = (spy_trend == 1) & (vix < 22)
    regime[mask_up_low_vix] = 1.0

    return regime


def _compute_tech_gate_series(signals):
    """
    計算每日的科技板塊門檻。

    v1.2 — 加入 tech_boost:
    - SOX > MA60 + mom > 10%     → 'boost'    (科技 slot ×1.5)
    - SOX > MA60 + mom > 5%      → 'strong'   (科技 slot ×1.2)
    - SOX > MA60                  → 'open'     (正常)
    - SOX < MA60, mom > -3%      → 'all_half'  (所有板塊半倉)
    - SOX < MA60, mom < -3%      → 'closed'    (科技禁止 + 其他半倉)
    """
    gate = pd.Series('open', index=signals.index)

    sox_trend = signals.get('sox_trend', pd.Series(1, index=signals.index))
    sox_mom = signals.get('sox_mom_20d', pd.Series(0.0, index=signals.index))

    # SOX > MA60: 強勢加碼
    sox_up = sox_trend == 1

    # SOX > MA60 + mom > 10% → boost
    gate[sox_up & (sox_mom > 0.10)] = 'boost'

    # SOX > MA60 + mom > 5% (but <= 10%) → strong
    gate[sox_up & (sox_mom > 0.05) & (sox_mom <= 0.10)] = 'strong'

    # SOX > MA60 + mom <= 5% → open (already default)

    # SOX < MA60
    sox_down = sox_trend == 0

    # SOX down + mom > -3% → all_half (所有板塊半倉，不只科技)
    gate[sox_down & (sox_mom >= -0.03)] = 'all_half'

    # SOX down + mom < -3% → closed (科技禁止，其他半倉)
    gate[sox_down & (sox_mom < -0.03)] = 'closed'

    return gate


def align_us_to_tw(us_signals, tw_dates):
    """
    將美股信號對齊到台股交易日。

    美股 t-1 日的信號用於台股 t 日的決策。

    Parameters
    ----------
    us_signals : pd.DataFrame
        美股信號 DataFrame
    tw_dates : pd.DatetimeIndex
        台股交易日期

    Returns
    -------
    pd.DataFrame
        對齊後的信號，使用前一個可用的美股數據
    """
    # 將美股日期 shift +1 天（美股昨天 → 台股今天）
    # 然後用 forward fill 對齊到台股交易日
    aligned = us_signals.reindex(tw_dates, method='ffill')
    # Shift by 1 to use t-1 US data for t TW decision
    aligned = aligned.shift(1)
    return aligned
