"""
twstk.data —【套件 1】歷史數據（純資料層，零策略）

統一的乾淨資料入口，下游回測與每日模擬都只透過這裡取得資料：

    from twstk.data import fetch_prices, liquid_universe, fetch_benchmark
    from twstk.data import fetch_us_signals
    from twstk.data import build_inst_flow_df, get_inst_flow_for_signals   # ★新版法人

來源：
- 行情 OHLCV / benchmark / 美股 regime：yfinance（線上）
- 三大法人 + 分點券商：新版 GitHub Pages
  https://appr1ciat1.github.io/tw-institutional-stocker/data
  （可用環境變數 TW_INST_BASE_URL 覆寫）

說明：採延遲匯入（PEP 562），讓「只用法人資料」的情境不必載入 yfinance。
"""

import importlib

# name -> 來源子模組
_EXPORTS = {
    # prices
    "fetch_prices": "twstk.data.prices",
    "PricePanel": "twstk.data.prices",
    # universe
    "liquid_universe": "twstk.data.universe",
    # benchmark
    "fetch_benchmark": "twstk.data.benchmark",
    "equal_weight_benchmark": "twstk.data.benchmark",
    "compute_excess_return": "twstk.data.benchmark",
    # us market
    "fetch_us_signals": "twstk.data.us_market",
    "align_us_to_tw": "twstk.data.us_market",
    # 借券賣出(SBL,法人空方)
    "fetch_sbl_balances": "twstk.data.short_sale",
    # institutional（新版）
    "fetch_inst_timeseries": "twstk.data.institutional",
    "fetch_inst_rankings": "twstk.data.institutional",
    "fetch_stock_three_inst_latest": "twstk.data.institutional",
    "build_inst_flow_df": "twstk.data.institutional",
    "build_inst_flow_windows": "twstk.data.institutional",
    "get_inst_flow_for_signals": "twstk.data.institutional",
    "fetch_broker_ranking": "twstk.data.institutional",
    "fetch_broker_stats": "twstk.data.institutional",
    "fetch_broker_trends": "twstk.data.institutional",
    "fetch_broker_trades_latest": "twstk.data.institutional",
    "fetch_target_broker_trades": "twstk.data.institutional",
    "fetch_main_force_latest": "twstk.data.institutional",
    "INST_BASE_URL": "twstk.data.institutional",
}

# 特殊別名：INST_BASE_URL 對應 institutional.BASE_URL
_ALIASES = {"INST_BASE_URL": "BASE_URL"}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        mod = importlib.import_module(_EXPORTS[name])
        attr = _ALIASES.get(name, name)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals()) + __all__)
