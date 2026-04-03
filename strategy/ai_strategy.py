"""
AI 多維度 Rank 橫向排名策略 (AI Ensemble Cross-Sectional Ranking)

核心設計理念 — 奧坎剃刀原則：
用四個「相對弱指標」的橫向百分位排名加總（滿分 4.0），
取代單一絕對門檻值的傳統技術指標，讓系統動態適應全天候市場。

四維度指標：
1. 20 日動能 (Momentum)  — 過去 20 天的價格漲幅
2. 60MA 乖離率 (Trend Bias) — 價格偏離 60 日均線程度
3. 5/20 日量能比 (Volume Surge) — 短期量能放大倍率
4. 20 日波動率倒數 (Stability) — 越穩定排名越高
"""

import yfinance as yf
import pandas as pd
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
    close_df, high_df, low_df, vol_df : tuple[pd.DataFrame]
        各為 (日期 x 股票代號) 的 DataFrame，已做 forward fill
    """
    print(f"📥 正在批次下載 {len(tickers)} 檔股票的 {days} 天歷史資料...")

    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    tw_tickers = [f"{t}.TW" for t in tickers]
    df = yf.download(tw_tickers, start=start_date, end=end_date, progress=False)

    data = {}
    for col in ['Close', 'High', 'Low', 'Volume']:
        if isinstance(df.columns, pd.MultiIndex):
            temp_df = df.xs(col, level=0, axis=1)
        else:
            # 只有一檔股票時不會是 MultiIndex
            temp_df = df[[col]]

        temp_df.columns = [str(c).replace('.TW', '') for c in temp_df.columns]
        data[col] = temp_df.ffill()

    print(f"   ✅ 下載完成，資料範圍：{data['Close'].index[0].strftime('%Y-%m-%d')} → {data['Close'].index[-1].strftime('%Y-%m-%d')}")
    return data['Close'], data['High'], data['Low'], data['Volume']


def engineer_features(close_df, vol_df):
    """
    計算 AI 多維度特徵並做橫向百分位排名。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣 (日期 x 股票代號)
    vol_df : pd.DataFrame
        成交量矩陣 (日期 x 股票代號)

    Returns
    -------
    total_score : pd.DataFrame
        各股票的 AI 綜合評分（0~4 之間），日期 x 股票
    ma_60 : pd.DataFrame
        60 日均線矩陣，用於進場信號過濾
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

    # === 橫向百分位排名 (Cross-Sectional Percentile Rank) ===
    # axis=1 代表「同一天比較所有股票」，pct=True 輸出 0~1 之間的百分位
    rank_mom = mom_20.rank(axis=1, pct=True)
    rank_trend = trend_bias.rank(axis=1, pct=True)
    rank_vol = vol_surge.rank(axis=1, pct=True)
    rank_stab = stability.rank(axis=1, pct=True)

    # === 等權加總（滿分 4.0）===
    total_score = rank_mom + rank_trend + rank_vol + rank_stab

    print("   ✅ 特徵計算完成")
    return total_score, ma_60
