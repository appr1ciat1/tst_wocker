"""
Benchmark 模組：提供基準對比曲線

支援：
1. 0050 (台灣 50 ETF) Buy-and-Hold
2. 等權持有策略池內所有股票
3. Excess Return 計算
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')


def fetch_benchmark(ticker='0050', days=800):
    """
    下載 Benchmark 的每日收盤價。

    Parameters
    ----------
    ticker : str
        Benchmark 代號（預設 0050 = 台灣 50 ETF）
    days : int
        回溯天數

    Returns
    -------
    benchmark_equity : pd.Series
        以 1.0 為起始的 buy-and-hold 淨值曲線
    """
    print(f"📈 下載 Benchmark: {ticker}.TW ({days} 天)...")

    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    df = yf.download(f"{ticker}.TW", start=start_date, end=end_date, progress=False)

    if df.empty:
        print(f"   ⚠️ 無法下載 {ticker} 資料")
        return pd.Series(dtype=float)

    close = df['Close']
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    benchmark_equity = close / close.iloc[0]

    print(f"   ✅ Benchmark 下載完成: {close.index[0].strftime('%Y-%m-%d')}"
          f" → {close.index[-1].strftime('%Y-%m-%d')}")
    return benchmark_equity


def equal_weight_benchmark(close_df):
    """
    計算等權持有所有池內股票的淨值曲線。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣

    Returns
    -------
    ew_equity : pd.Series
        等權持有淨值曲線（以 1.0 為起始）
    """
    daily_returns = close_df.pct_change()
    ew_return = daily_returns.mean(axis=1)  # 每日等權平均報酬
    ew_equity = (1 + ew_return).cumprod()
    ew_equity.iloc[0] = 1.0
    return ew_equity


def compute_excess_return(strategy_equity, benchmark_equity):
    """
    計算策略相對 Benchmark 的超額累積報酬。

    Parameters
    ----------
    strategy_equity : pd.Series
        策略淨值曲線
    benchmark_equity : pd.Series
        Benchmark 淨值曲線

    Returns
    -------
    excess : pd.Series
        累積超額報酬
    """
    # 對齊日期
    common_idx = strategy_equity.index.intersection(benchmark_equity.index)
    if len(common_idx) == 0:
        return pd.Series(dtype=float)

    strat = strategy_equity.loc[common_idx]
    bench = benchmark_equity.loc[common_idx]

    # 累積超額
    strat_norm = strat / strat.iloc[0]
    bench_norm = bench / bench.iloc[0]
    excess = strat_norm - bench_norm

    return excess
