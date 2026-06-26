"""
三大法人籌碼因子模組（相容 shim）

⚠️ 本檔已升級為「新版」資料來源。實作搬到 `twstk.data.institutional`，
   來源由舊版 voidful Pages 改為新版：
       https://appr1ciat1.github.io/tw-institutional-stocker/data

   舊呼叫端（ai_report.py / paper_trade.py / strategy.core_holdings）
   無需改動，import 本模組即自動使用新版資料。
   如需臨時改來源，可設環境變數 TW_INST_BASE_URL。
"""

from twstk.data.institutional import (  # noqa: F401
    BASE_URL,
    fetch_inst_timeseries,
    fetch_inst_rankings,
    fetch_stock_three_inst_latest,
    build_inst_flow_df,
    build_inst_flow_windows,
    get_inst_flow_for_signals,
    # 新版新增：分點券商
    fetch_broker_ranking,
    fetch_broker_stats,
    fetch_broker_trends,
    fetch_broker_trades_latest,
    fetch_target_broker_trades,
)

__all__ = [
    "BASE_URL",
    "fetch_inst_timeseries",
    "fetch_inst_rankings",
    "fetch_stock_three_inst_latest",
    "build_inst_flow_df",
    "build_inst_flow_windows",
    "get_inst_flow_for_signals",
    "fetch_broker_ranking",
    "fetch_broker_stats",
    "fetch_broker_trends",
    "fetch_broker_trades_latest",
    "fetch_target_broker_trades",
]
