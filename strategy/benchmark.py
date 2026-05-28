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


def fetch_benchmark(ticker='0050', days=800, start_date=None, end_date=None):
    """
    下載 Benchmark 的每日收盤價。

    Parameters
    ----------
    ticker : str
        Benchmark 代號（預設 0050 = 台灣 50 ETF）
    days : int
        回溯天數。若提供 start_date，則忽略此參數。
    start_date, end_date : str or datetime, optional
        明確指定 benchmark 區間。

    Returns
    -------
    benchmark_equity : pd.Series
        以 1.0 為起始的 buy-and-hold 淨值曲線
    """
    if end_date is not None:
        end_dt = pd.Timestamp(end_date)
    else:
        end_dt = pd.Timestamp(datetime.today())

    if start_date is not None:
        start_dt = pd.Timestamp(start_date)
        range_label = f"{start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}"
    else:
        start_dt = end_dt - timedelta(days=days)
        range_label = f"{days} 天"

    print(f"📈 下載 Benchmark: {ticker}.TW ({range_label})...")

    df = yf.download(f"{ticker}.TW", start=start_dt, end=end_dt, progress=False)

    if df.empty:
        print(f"   ⚠️ 無法下載 {ticker} 資料")
        return pd.Series(dtype=float)

    close = df['Close']
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    close = pd.to_numeric(close, errors='coerce').replace([np.inf, -np.inf], np.nan)
    dropped_count = int(close.isna().sum())
    close = close.dropna()
    if close.empty:
        print(f"   ⚠️ {ticker} 無有效收盤價資料")
        return pd.Series(dtype=float)

    if dropped_count:
        print(f"   ℹ️ 已忽略 {dropped_count} 筆無效收盤價")

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
