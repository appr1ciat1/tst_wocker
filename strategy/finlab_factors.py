"""
FinLab 啟發因子模組 (FinLab-Inspired Factors)

實作 FinLab 平台常見的三大因子：
1. RSI 動量排名 — 類似 data.indicator('RSI', 20).is_largest(20)
2. N 日創新高突破 — 類似 close >= close.rolling(300).max()
3. PE/PB 價值因子 — 類似 PE < 10 + PB < 1.5 複合分數

所有因子輸出為「橫截面百分位排名 (0~1)」，可直接乘以權重
加入現有的 total_score 系統。

參考文獻：
- FinLab 官方文件：均線趨勢過濾、RSI 選股、創新高突破
- FinLab FB 社群：價值+動量「雙渦輪」策略
"""

import pandas as pd
import numpy as np


def compute_rsi(close_df, period=20):
    """
    計算全市場 RSI (Relative Strength Index)。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣 (日期 x 股票)
    period : int
        RSI 計算期數（預設 20，FinLab 常用 20）

    Returns
    -------
    rsi_df : pd.DataFrame
        RSI 值 (0~100)，與 close_df 同形狀
    """
    delta = close_df.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))

    # 使用 exponential moving average（與 FinLab/TA-Lib 一致）
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))

    return rsi


def compute_rsi_rank(close_df, period=20, universe_mask=None):
    """
    計算全市場 RSI 的橫截面百分位排名。

    FinLab 用法：data.indicator('RSI', 20).is_largest(20)
    我們的用法：rank(RSI) → 百分位 (0~1)，RSI 越高排名越前。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    period : int
        RSI 期數
    universe_mask : pd.DataFrame (bool), optional
        動態 Universe 遮罩

    Returns
    -------
    rank_rsi : pd.DataFrame
        橫截面百分位排名 (0~1)
    """
    rsi = compute_rsi(close_df, period)

    if universe_mask is not None:
        rsi = rsi.where(universe_mask)

    rank_rsi = rsi.rank(axis=1, pct=True)
    return rank_rsi


def compute_breakout(close_df, window=300):
    """
    計算 N 日創新高突破信號。

    FinLab 用法：position = close >= close.rolling(300).max()
    我們的用法：計算「距離 N 日最高的百分比」作為連續分數。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    window : int
        回溯窗口（預設 300 日，FinLab 預設）

    Returns
    -------
    breakout_score : pd.DataFrame
        距離 N 日最高的比例 (0~1)，1.0 表示正在創新高
    """
    rolling_max = close_df.rolling(window, min_periods=int(window * 0.5)).max()
    # 距離 N 日新高的比例，越接近 1 越強
    breakout_score = close_df / (rolling_max + 1e-10)
    return breakout_score


def compute_breakout_rank(close_df, window=300, universe_mask=None):
    """
    計算創新高突破的橫截面百分位排名。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    window : int
        回溯窗口
    universe_mask : pd.DataFrame (bool), optional

    Returns
    -------
    rank_breakout : pd.DataFrame
        橫截面百分位排名 (0~1)
    """
    breakout = compute_breakout(close_df, window)

    if universe_mask is not None:
        breakout = breakout.where(universe_mask)

    rank_breakout = breakout.rank(axis=1, pct=True)
    return rank_breakout


def fetch_value_data(tickers, close_df):
    """
    從 yfinance 取得 PE/PB 快照數據，填入 DataFrame。

    注意：yfinance 的 .info 只提供最新快照，
    無法取得歷史 PE/PB。因此我們用「靜態快照 + 價格變動反推」
    來近似歷史 PB/PE。

    Parameters
    ----------
    tickers : list[str]
        台股代號列表
    close_df : pd.DataFrame
        收盤價矩陣（用於反推歷史）

    Returns
    -------
    pb_df : pd.DataFrame
        PB（股價淨值比）矩陣。靜態 BPS 配合每日收盤價。
    pe_df : pd.DataFrame
        PE（本益比）矩陣。靜態 EPS 配合每日收盤價。
    """
    import yfinance as yf

    print("📊 正在取得 PE/PB 價值因子數據...")

    # 取得最新快照
    bps_dict = {}  # Book value per share
    eps_dict = {}  # Earnings per share

    batch_size = 20
    tw_tickers = [f"{t}.TW" for t in tickers]

    for i in range(0, len(tw_tickers), batch_size):
        batch = tw_tickers[i:i+batch_size]
        for tw_t in batch:
            try:
                info = yf.Ticker(tw_t).info
                t = tw_t.replace('.TW', '')

                # Book Value Per Share → 用來算 PB
                bvps = info.get('bookValue', None)
                if bvps and bvps > 0:
                    bps_dict[t] = bvps

                # Trailing EPS → 用來算 PE
                eps = info.get('trailingEps', None)
                if eps and eps > 0:
                    eps_dict[t] = eps

            except Exception:
                continue

    print(f"   ✅ 取得 BPS: {len(bps_dict)} 檔, EPS: {len(eps_dict)} 檔")

    # 建立歷史 PB/PE DataFrame
    # PB = 每日收盤價 / BPS（假設 BPS 在回測期間穩定）
    pb_df = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)
    pe_df = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)

    for t in close_df.columns:
        if t in bps_dict and bps_dict[t] > 0:
            pb_df[t] = close_df[t] / bps_dict[t]
        if t in eps_dict and eps_dict[t] > 0:
            pe_df[t] = close_df[t] / eps_dict[t]

    return pb_df, pe_df


def compute_value_rank(close_df, pb_df=None, pe_df=None,
                       universe_mask=None, tickers=None):
    """
    計算價值因子的橫截面百分位排名。

    FinLab 價值策略：PE < 10, PB < 1.5
    我們的實作：PB 和 PE 越低越好 → 使用反向排名。

    如果 pb_df/pe_df 為 None，會自動呼叫 fetch_value_data()。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    pb_df : pd.DataFrame, optional
        PB 矩陣
    pe_df : pd.DataFrame, optional
        PE 矩陣
    universe_mask : pd.DataFrame (bool), optional
    tickers : list[str], optional
        股票列表（用於自動取得數據）

    Returns
    -------
    rank_value : pd.DataFrame
        價值因子橫截面排名 (0~1)，值越低排名越前
    """
    if pb_df is None or pe_df is None:
        if tickers is None:
            tickers = list(close_df.columns)
        pb_df, pe_df = fetch_value_data(tickers, close_df)

    # 價值越低越好 → ascending=False 讓低 PB/PE 得到高排名
    def _rank_ascending_false(df):
        """低值 = 高排名。"""
        if universe_mask is not None:
            df = df.where(universe_mask)
        return df.rank(axis=1, pct=True, ascending=False)

    rank_pb = _rank_ascending_false(pb_df)
    rank_pe = _rank_ascending_false(pe_df)

    # 複合價值分數：PB 和 PE 等權平均
    # 處理 NaN：只要有一個有值就用
    rank_value = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)

    both_valid = rank_pb.notna() & rank_pe.notna()
    pb_only = rank_pb.notna() & rank_pe.isna()
    pe_only = rank_pb.isna() & rank_pe.notna()

    rank_value[both_valid] = (rank_pb[both_valid] + rank_pe[both_valid]) / 2
    rank_value[pb_only] = rank_pb[pb_only]
    rank_value[pe_only] = rank_pe[pe_only]

    return rank_value


def compute_revenue_momentum(close_df, period=60, universe_mask=None):
    """
    用價格動能近似「月營收動能」。

    FinLab 原始方法需要月營收 API，這裡用 60 日價格變動率
    作為營收成長的代理指標（正相關性高）。

    FinLab 的「MEMORY.md 6 條鐵律」中：
    - close/close.shift(60) - 1 > 0 動量過濾，CAGR 穩定提升

    Parameters
    ----------
    close_df : pd.DataFrame
    period : int
        回溯期（預設 60 日 ≈ 3 個月）
    universe_mask : pd.DataFrame (bool), optional

    Returns
    -------
    rank_rev_mom : pd.DataFrame
        橫截面百分位排名
    """
    rev_momentum = close_df / close_df.shift(period) - 1

    if universe_mask is not None:
        rev_momentum = rev_momentum.where(universe_mask)

    rank_rev_mom = rev_momentum.rank(axis=1, pct=True)
    return rank_rev_mom
