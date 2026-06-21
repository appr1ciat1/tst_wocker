"""
strategies.hybrid_tiered_v9 — v9 Hybrid Tiered overlay（忠實版）

v9 = 底層 alpha（任一 SignalProducer，預設 v8.5 動量）
     + Portfolio Volatility Targeting + Core-Satellite 分層風險預算 overlay。

忠實重現：直接用 strategy.event_backtest.EventDrivenBacktester 的內建
hybrid_tiered 路徑（與 build_v3_production_backtester / compare_v85_v9 相同設定），
並套用 strategy.portfolio_vol_target.v3_production_kwargs() 的 V3 生產參數。

overlay 可套在任何「能產生橫向評分」的底層策略（SignalProducer）上：
    get_strategy("hybrid_tiered_v9", base="momentum_v85")

註：tiered 邏輯內建於 v8.5 事件引擎，故底層需為 SignalProducer（如 momentum_v85）。
   SR v2 採不同引擎、無橫向評分，無法直接套此 overlay（會明確報錯）。
"""

from typing import Tuple

import pandas as pd

from strategies.base import EngineStrategy, SignalProducer, MarketData, ExecConfig
from strategies.registry import register, get_strategy
from strategy.event_backtest import EventDrivenBacktester
from strategy.portfolio_vol_target import v3_production_kwargs


@register("hybrid_tiered_v9")
class HybridTieredV9(EngineStrategy):
    description = "v9 Hybrid Tiered overlay（Core-Satellite + 波動目標）疊在底層 SignalProducer 上"

    DEFAULT_CORE = ["2330", "2454", "2308", "2317", "3008"]

    def __init__(self, base: str = "momentum_v85", core_tickers=None,
                 target_ann_vol: float = 0.15, regime_floor: float = 0.10,
                 gap_filter_atr: float = 1.5, position_size: float = 0.10,
                 tp_atr: float = 4.0, sl_atr: float = 3.0, hold_days: int = 20,
                 **params):
        super().__init__(base=base, core_tickers=core_tickers,
                         target_ann_vol=target_ann_vol, **params)
        self._base_name = base
        self.core_tickers = list(core_tickers) if core_tickers else list(self.DEFAULT_CORE)
        self.target_ann_vol = target_ann_vol
        self.regime_floor = regime_floor
        self.gap_filter_atr = gap_filter_atr
        self.position_size = position_size
        self.tp_atr, self.sl_atr, self.hold_days = tp_atr, sl_atr, hold_days

        # 解析底層策略，並繼承其資料需求（如 us_signals）
        self._base = get_strategy(base)
        if not isinstance(self._base, SignalProducer):
            raise TypeError(
                f"v9 overlay 的底層 '{base}' 必須是 SignalProducer（能產生橫向評分），"
                f"例如 momentum_v85。SR v2 等自帶引擎策略無法直接套用。"
            )
        self.requires = frozenset(getattr(self._base, "requires", frozenset()))

    def adjust_score(self, total_score: pd.DataFrame, data: MarketData) -> pd.DataFrame:
        """子類可覆寫：在送入引擎前微調選股評分（例如加入借券 tilt）。預設不動。"""
        return total_score

    def run_engine(self, data: MarketData,
                   exec_cfg: ExecConfig) -> Tuple[pd.DataFrame, pd.DataFrame]:
        bundle = self._base.prepare(data)
        total_score = self.adjust_score(bundle.total_score, data)

        # 與 build_v3_production_backtester 相同的 tiered 引擎設定
        bt = EventDrivenBacktester(
            tp_sl_mode="atr",
            tp_atr_mult=self.tp_atr, sl_atr_mult=self.sl_atr,
            max_hold_days=self.hold_days,
            initial_capital=exec_cfg.initial_capital,
            position_size=self.position_size,
            regime_filter=True, regime_graduated=True,
            regime_floor=self.regime_floor,
            gap_filter_atr=self.gap_filter_atr,
            breadth_regime=True,
            hybrid_tiered=True,
            core_tickers=self.core_tickers,
            target_ann_vol=self.target_ann_vol,
            buy_cost=exec_cfg.buy_cost, sell_cost=exec_cfg.sell_cost,
            corr_filter=0.0, gap_aware_sizing=False, slippage=exec_cfg.slippage,
            **v3_production_kwargs(),
        )
        return bt.run(
            total_score, data.close, data.open, data.high, data.low,
            bundle.ma_long,
            top_k=exec_cfg.top_k, threshold=exec_cfg.threshold,
            market_close=data.market_close, vol_df=data.volume,
            universe_mask=data.universe_mask,
        )
