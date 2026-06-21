"""
twstk.data.prices — 台股 OHLCV 歷史行情（yfinance 日線）

純資料層：只負責把行情抓回來、整理成乾淨的 (日期 × 代號) 矩陣，
不含任何選股 / 訊號 / 策略邏輯。

底層沿用既有且已驗證的 `strategy.ai_strategy.fetch_panel_data`
（純資料函式），這裡只提供乾淨的命名與型別封裝。
"""

from dataclasses import dataclass

import pandas as pd

from strategy.ai_strategy import fetch_panel_data as _fetch_panel_data


@dataclass
class PricePanel:
    """一次抓回的完整行情面板（皆為 日期 × 代號 的 DataFrame）。"""
    close: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame

    @property
    def tickers(self):
        return list(self.close.columns)

    @property
    def dates(self):
        return self.close.index


def fetch_prices(tickers, days=800, start_date=None, end_date=None) -> PricePanel:
    """
    批次下載多檔台股日線 OHLCV。

    Parameters
    ----------
    tickers : list[str]
        台股代號，例如 ['2330', '2317']
    days : int
        回溯天數（start_date 為 None 時使用）
    start_date, end_date : str | datetime, optional
        明確指定區間。start_date 優先於 days。

    Returns
    -------
    PricePanel
    """
    close, open_, high, low, vol = _fetch_panel_data(
        tickers, days=days, start_date=start_date, end_date=end_date,
    )
    return PricePanel(close=close, open=open_, high=high, low=low, volume=vol)
