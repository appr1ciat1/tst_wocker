"""
twstk.data.universe — 動態流動性 Universe（可投資池）

屬資料層：依「過去 N 日平均成交額 Top-K」決定每日可投資池，
不含任何選股偏好或策略，僅做流動性過濾。
"""

import pandas as pd

from strategy.ai_strategy import build_liquid_universe as _build_liquid_universe


def liquid_universe(close_df: pd.DataFrame, vol_df: pd.DataFrame,
                    top_n: int = 60, lookback: int = 20) -> pd.DataFrame:
    """
    回傳 (日期 × 代號) 的布林遮罩，True 代表當日在流動性池內。

    Parameters
    ----------
    close_df, vol_df : pd.DataFrame
        收盤價、成交量矩陣
    top_n : int
        每日池大小（預設 60）
    lookback : int
        成交額均值回溯期（預設 20）
    """
    return _build_liquid_universe(close_df, vol_df, top_n=top_n, lookback=lookback)
