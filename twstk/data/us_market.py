"""
twstk.data.us_market — 美股 regime 資料（SPY / VIX / SOX）

純資料層 facade，沿用 strategy.us_market 的已驗證實作。
回傳的 DataFrame 含 macro_regime、tech_gate 等欄位，供策略層自行取用。
"""

from strategy.us_market import fetch_us_signals, align_us_to_tw

__all__ = ["fetch_us_signals", "align_us_to_tw"]
