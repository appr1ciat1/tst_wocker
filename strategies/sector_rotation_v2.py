"""
strategies.sector_rotation_v2 — 板塊輪動 v2（忠實版，自帶事件引擎）

忠實重現既有 SR v2：三層架構
  1. 美股 Macro Regime（SPY/VIX → 曝險、SOX → 科技門檻）
  2. 板塊資金流（10/15/20d）選強勢板塊
  3. 板塊內動量選股
用 strategy.sector_rotation_backtest.SectorRotationBacktester 跑回測。

需要美股訊號 → requires={'us_signals'}；資料層會自動抓取並對齊台股交易日。
"""

from typing import Tuple

import pandas as pd

from strategies.base import EngineStrategy, MarketData, ExecConfig
from strategies.registry import register
from strategy.sector_rotation_backtest import SectorRotationBacktester


@register("sector_rotation_v2")
class SectorRotationV2(EngineStrategy):
    description = "SR v2 板塊輪動（美股 regime + 板塊資金流 + 板塊內動量）"
    requires = frozenset({"us_signals"})

    DEFAULTS = {
        "tp_atr": 4.0,
        "sl_atr": 3.0,
        "hold_days": 20,
        "top_sectors": 3,
        "stocks_per_sector": 3,
    }

    def _p(self):
        return {**self.DEFAULTS, **self.params}

    def run_engine(self, data: MarketData,
                   exec_cfg: ExecConfig) -> Tuple[pd.DataFrame, pd.DataFrame]:
        if data.us_signals is None:
            raise RuntimeError(
                "SR v2 需要美股訊號（us_signals）。請確認資料層有抓取並對齊。"
            )
        p = self._p()
        bt = SectorRotationBacktester(
            initial_capital=exec_cfg.initial_capital,
            tp_atr_mult=p["tp_atr"], sl_atr_mult=p["sl_atr"],
            max_hold_days=p["hold_days"],
            top_sectors=p["top_sectors"],
            stocks_per_sector=p["stocks_per_sector"],
            buy_cost=exec_cfg.buy_cost, sell_cost=exec_cfg.sell_cost,
            slippage=exec_cfg.slippage,
        )
        return bt.run(
            data.close, data.open, data.high, data.low, data.volume,
            data.us_signals, data.universe_mask,
        )
