"""
AI 多維度 Rank 橫向排名策略 (AI Ensemble Cross-Sectional Ranking) — v2

核心設計理念 — 奧坎剃刀原則：
用四個「相對弱指標」的橫向百分位排名加總（滿分 4.0），
取代單一絕對門檻值的傳統技術指標，讓系統動態適應全天候市場。

v2 改進：
- 新增 open_df 供 t+1 open 進場
- 新增 ATR 計算供波動度自適應 TP/SL 與 position sizing
- 新增 dynamic liquid universe 支援全 TWSE 動態排名
- 橫向排名改為 universe-masked（只在當日 liquid universe 中排序）

四維度指標：
1. 20 日動能 (Momentum)  — 過去 20 天的價格漲幅
2. 60MA 乖離率 (Trend Bias) — 價格偏離 60 日均線程度
3. 5/20 日量能比 (Volume Surge) — 短期量能放大倍率
4. 20 日波動率倒數 (Stability) — 越穩定排名越高
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')


def fetch_panel_data(tickers, days=800):
    """
    批次下載多檔台股的 OHLCV 日線資料。

    Parameters
    ----------
    tickers : list[str]
        台股代號列表，例如 ['2330', '2317', '2454']
    days : int
        回溯天數，預設 800 天（約 3 年交易日）

    Returns
    -------
    close_df, open_df, high_df, low_df, vol_df : tuple[pd.DataFrame]
        各為 (日期 x 股票代號) 的 DataFrame，已做 forward fill
    """
    print(f"📥 正在批次下載 {len(tickers)} 檔股票的 {days} 天歷史資料...")

    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    tw_tickers = [f"{t}.TW" for t in tickers]

    # yfinance 批次下載有大小限制，分批處理
    batch_size = 50
    all_dfs = []
    for batch_start in range(0, len(tw_tickers), batch_size):
        batch = tw_tickers[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(tw_tickers) + batch_size - 1) // batch_size
        print(f"   📦 下載批次 {batch_num}/{total_batches} ({len(batch)} 檔)...")
        df = yf.download(batch, start=start_date, end=end_date, progress=False)
        if not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        raise RuntimeError("無法下載任何資料")

    # 合併所有批次
    if len(all_dfs) == 1:
        df = all_dfs[0]
    else:
        df = pd.concat(all_dfs, axis=1)

    data = {}
    for col in ['Close', 'Open', 'High', 'Low', 'Volume']:
        if isinstance(df.columns, pd.MultiIndex):
            try:
                temp_df = df.xs(col, level=0, axis=1)
            except KeyError:
                print(f"   ⚠️ 欄位 {col} 不存在，跳過")
                continue
        else:
            temp_df = df[[col]]

        temp_df.columns = [str(c).replace('.TW', '') for c in temp_df.columns]
        data[col] = temp_df.ffill()

    print(f"   ✅ 下載完成，資料範圍：{data['Close'].index[0].strftime('%Y-%m-%d')}"
          f" → {data['Close'].index[-1].strftime('%Y-%m-%d')}"
          f"，共 {len(data['Close'].columns)} 檔")
    return data['Close'], data['Open'], data['High'], data['Low'], data['Volume']


def build_liquid_universe(close_df, vol_df, top_n=50, lookback=20):
    """
    建立動態流動性 Universe。

    每日取「過去 lookback 日平均成交額 Top-N」作為當日可投資池。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    vol_df : pd.DataFrame
        成交量矩陣
    top_n : int
        每日 universe 大小
    lookback : int
        成交額均值回溯期

    Returns
    -------
    universe_mask : pd.DataFrame (bool)
        (日期 x 股票) 的布林矩陣，True 代表當日在 universe 中
    """
    print(f"🌐 建立動態流動性 Universe (Top-{top_n}, 回溯 {lookback} 日)...")

    # 平均成交額 = 收盤價 × 成交量 的 rolling mean
    turnover = (close_df * vol_df).rolling(lookback).mean()

    # 每日取 top_n
    universe_mask = turnover.rank(axis=1, ascending=False) <= top_n

    # 確保 NaN 的位置不被選入
    universe_mask = universe_mask & close_df.notna() & (close_df > 0)

    avg_size = universe_mask.sum(axis=1).mean()
    print(f"   ✅ 動態 Universe 建立完成，平均每日 {avg_size:.0f} 檔")
    return universe_mask


def engineer_features(close_df, vol_df, universe_mask=None):
    """
    計算 AI 多維度特徵並做橫向百分位排名。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣 (日期 x 股票代號)
    vol_df : pd.DataFrame
        成交量矩陣 (日期 x 股票代號)
    universe_mask : pd.DataFrame (bool), optional
        動態 Universe 遮罩。若提供，只在當日 universe 中做排名。

    Returns
    -------
    total_score : pd.DataFrame
        各股票的 AI 綜合評分（0~4 之間），日期 x 股票
    ma_60 : pd.DataFrame
        60 日均線矩陣，用於進場信號過濾
    atr_df : pd.DataFrame
        20 日 ATR 矩陣，用於自適應 TP/SL 與 position sizing
    """
    print("🧠 正在計算多維度弱特徵與 Rank 排名...")

    # === 原始指標計算 ===
    # 1. 20 日動能：今天收盤 / 20 天前收盤
    mom_20 = close_df / close_df.shift(20)

    # 2. 60MA 乖離率：價格偏離 60 日均線的幅度
    ma_60 = close_df.rolling(60).mean()
    trend_bias = close_df / ma_60

    # 3. 量能爆發比：5 日均量 / 20 日均量
    vol_surge = vol_df.rolling(5).mean() / (vol_df.rolling(20).mean() + 1e-8)

    # 4. 穩定度：波動率的倒數（越穩定越好）
    volatility = close_df.pct_change().rolling(20).std()
    stability = 1 / (volatility + 1e-8)

    # === ATR 計算 (用於 TP/SL 與 sizing) ===
    high_low = close_df.rolling(2).max() - close_df.rolling(2).min()
    # 近似 ATR：使用收盤價的 rolling range（因為 engineer_features 不接收 high/low）
    # 精確 ATR 將在 backtest 中使用 high_df/low_df 計算
    atr_df = close_df.pct_change().abs().rolling(20).mean() * close_df

    # === 橫向百分位排名 (Cross-Sectional Percentile Rank) ===
    if universe_mask is not None:
        # 只在當日 universe 中做排名
        masked_mom = mom_20.where(universe_mask)
        masked_trend = trend_bias.where(universe_mask)
        masked_vol = vol_surge.where(universe_mask)
        masked_stab = stability.where(universe_mask)

        rank_mom = masked_mom.rank(axis=1, pct=True)
        rank_trend = masked_trend.rank(axis=1, pct=True)
        rank_vol = masked_vol.rank(axis=1, pct=True)
        rank_stab = masked_stab.rank(axis=1, pct=True)
    else:
        # 全池排名（靜態池模式）
        rank_mom = mom_20.rank(axis=1, pct=True)
        rank_trend = trend_bias.rank(axis=1, pct=True)
        rank_vol = vol_surge.rank(axis=1, pct=True)
        rank_stab = stability.rank(axis=1, pct=True)

    # === 等權加總（滿分 4.0）===
    total_score = rank_mom + rank_trend + rank_vol + rank_stab

    print("   ✅ 特徵計算完成")
    return total_score, ma_60, atr_df
