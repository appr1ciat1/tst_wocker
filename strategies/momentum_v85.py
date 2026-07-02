"""
strategies.momentum_v85 — v8.5 橫向動量（忠實版，自帶事件引擎）

忠實重現既有 v8.5：用 strategy.ai_strategy.engineer_features 產生
Mom×3 + Trend×1 評分，餵進 strategy.event_backtest.EventDrivenBacktester
（hybrid_tiered=False，ATR 停利停損 4.0/3.0、持有 20D、Top-7、Gap 1.5、regime filter）。

同時實作 SignalProducer.prepare()，讓 v9 overlay（hybrid_tiered_v9）能取用同一組訊號。
"""

from typing import Tuple

import pandas as pd

from strategies.base import (
    EngineStrategy, SignalProducer, MarketData, SignalBundle, ExecConfig,
)
from strategies.registry import register
from strategy.ai_strategy import engineer_features
from strategy.event_backtest import EventDrivenBacktester


@register("momentum_v85")
class MomentumV85(EngineStrategy, SignalProducer):
    description = "v8.5 橫向動量 (Mom×3 + Trend×1)，忠實事件引擎（ATR 4/3, Hold20, Top7）"

    DEFAULTS = {
        "ma_period": 60,
        "multi_ma": False,
        "inst_flow_weight": 0.0,
        "tp_atr": 4.0,
        "sl_atr": 3.0,
        "hold_days": 20,
        "gap_filter_atr": 1.5,
        "regime_filter": True,
    }

    def _p(self):
        return {**self.DEFAULTS, **self.params}

    # ── SignalProducer：產生評分（供自身與 v9 overlay 使用）──
    def prepare(self, data: MarketData) -> SignalBundle:
        p = self._p()
        total_score, ma_long, atr_df, short_ma = engineer_features(
            data.close, data.volume, data.universe_mask,
            ma_period=p["ma_period"],
            multi_ma=p["multi_ma"],
            inst_flow_weight=p["inst_flow_weight"],
            inst_flow_df=data.inst_flow_df,
            market_close=data.market_close,
        )
        return SignalBundle(total_score=total_score, ma_long=ma_long,
                            atr_df=atr_df, short_ma=short_ma, params=dict(p))

    # ── EngineStrategy：忠實回測 ──
    def run_engine(self, data: MarketData,
                   exec_cfg: ExecConfig) -> Tuple[pd.DataFrame, pd.DataFrame]:
        p = self._p()
        bundle = self.prepare(data)
        bt = EventDrivenBacktester(
            tp_sl_mode="atr",
            tp_atr_mult=p["tp_atr"], sl_atr_mult=p["sl_atr"],
            max_hold_days=p["hold_days"],
            initial_capital=exec_cfg.initial_capital,
            regime_filter=p["regime_filter"],
            gap_filter_atr=p["gap_filter_atr"],
            hybrid_tiered=False,
            buy_cost=exec_cfg.buy_cost, sell_cost=exec_cfg.sell_cost,
            slippage=exec_cfg.slippage,
            corr_select_max=p.get("corr_select_max", 0.0),
            corr_select_window=p.get("corr_select_window", 60),
            corr_select_cap=p.get("corr_select_cap", 1),
        )
        return bt.run(
            bundle.total_score, data.close, data.open, data.high, data.low,
            bundle.ma_long,
            top_k=exec_cfg.top_k, threshold=exec_cfg.threshold,
            market_close=data.market_close, vol_df=data.volume,
            universe_mask=data.universe_mask,
        )
