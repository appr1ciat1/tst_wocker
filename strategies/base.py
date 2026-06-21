"""
strategies.base — 策略插件介面（抽換用接縫）

這是「三層基礎設施」與「各種策略」之間唯一的接縫。
回測層（twstk.backtest）與每日模擬層（twstk.paper）只認得這裡的介面。

兩種執行型態
============
1. WeightStrategy —「目標權重型」（推薦給未來研究的全新策略）
   只要回答：每個交易日想持有哪些股票、各佔多少權重？
   → 走共用的權重成交核心（twstk.portfolio），回測與模擬語意一致。
   評分型（SignalStrategy）是其特例：實作 prepare()→SignalBundle，框架自動轉等權 Top-K。

2. EngineStrategy —「自帶引擎型」（用來「忠實」重現既有 v8.5 / SR v2 / v9）
   策略自己用既有的事件驅動引擎跑回測（ATR 停利停損、跳空、tiered overlay…），
   回傳 (trades_df, equity_df)。能重現 README 上的數字。

執行層（twstk）會依策略型態自動分派；策略作者只需選對基底類別。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
import pandas as pd


# ── 資料束 ──────────────────────────────────────────────────────
@dataclass
class MarketData:
    """傳給策略的資料束（全部來自 twstk.data，純資料）。"""
    close: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame
    market_close: Optional[pd.Series] = None        # 大盤(0050)，供 regime
    universe_mask: Optional[pd.DataFrame] = None     # 流動性池遮罩
    us_signals: Optional[pd.DataFrame] = None        # 美股 regime（已對齊台股交易日）
    inst_flow_df: Optional[pd.DataFrame] = None      # ★三大法人變化（新版）
    inst_ratio_df: Optional[pd.DataFrame] = None     # ★三大法人比重（新版）
    short_sale_df: Optional[pd.DataFrame] = None     # ★借券賣出餘額（SBL,法人空方）


@dataclass
class SignalBundle:
    """評分型策略的中間輸出。"""
    total_score: pd.DataFrame                 # 日期 × 代號，越高越優先
    ma_long: Optional[pd.DataFrame] = None    # 趨勢過濾：close > ma_long
    atr_df: Optional[pd.DataFrame] = None
    short_ma: Optional[pd.DataFrame] = None
    params: dict = field(default_factory=dict)  # top_k / threshold ...


@dataclass
class ExecConfig:
    """執行層級設定（資金 / 成本 / 滑價），由回測或模擬層傳給 EngineStrategy。"""
    initial_capital: float = 1_000_000
    buy_cost: float = 0.001425
    sell_cost: float = 0.004425
    slippage: float = 0.0
    top_k: int = 7
    threshold: float = 2.0


# ── 策略基底 ────────────────────────────────────────────────────
class Strategy(ABC):
    """所有策略插件的最上層基底。"""

    name: str = "base"
    description: str = ""
    #: 宣告需要哪些選用資料（讓資料層只抓必要的）。可含 'us_signals' / 'inst_flow'。
    requires: set = frozenset()

    def __init__(self, **params):
        self.params = params

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"


# ── 型態 1：目標權重型 ──────────────────────────────────────────
class WeightStrategy(Strategy):
    """目標權重型：實作 target_weights()，走共用權重成交核心。"""

    @abstractmethod
    def target_weights(self, data: MarketData) -> pd.DataFrame:
        """回傳 (日期 × 代號) 目標權重矩陣；0 = 不持有，列和 ≤ 1。"""
        raise NotImplementedError


class SignalProducer(ABC):
    """可產生橫向評分的能力（供 SignalStrategy 與 v9 overlay 取用）。"""

    @abstractmethod
    def prepare(self, data: MarketData) -> SignalBundle:
        raise NotImplementedError


def signals_to_weights(bundle: SignalBundle, data: MarketData,
                       top_k: int = 7, threshold: float = 2.0) -> pd.DataFrame:
    """評分 + 趨勢/流動性過濾 → 等權 Top-K 目標權重。"""
    score = bundle.total_score
    eligible = score >= threshold
    if bundle.ma_long is not None:
        eligible = eligible & (data.close.reindex_like(score) > bundle.ma_long)
    if data.universe_mask is not None:
        eligible = eligible & data.universe_mask.reindex_like(score).fillna(False)

    masked = score.where(eligible)
    ranks = masked.rank(axis=1, ascending=False)
    selected = (ranks <= top_k) & masked.notna()

    w = selected.astype(float)
    row_sum = w.sum(axis=1).replace(0, np.nan)
    return w.div(row_sum, axis=0).fillna(0.0)


class SignalStrategy(WeightStrategy, SignalProducer):
    """
    評分型「權重」策略：實作 prepare()，框架自動轉等權 Top-K 目標權重。
    （注意：這是 target_weights 路徑，非事件引擎；要忠實重現 ATR 數字請用 EngineStrategy。）
    """

    def target_weights(self, data: MarketData) -> pd.DataFrame:
        bundle = self.prepare(data)
        p = bundle.params
        return signals_to_weights(
            bundle, data,
            top_k=p.get("top_k", 7),
            threshold=p.get("threshold", 2.0),
        )


# ── 型態 2：自帶引擎型 ──────────────────────────────────────────
class EngineStrategy(Strategy):
    """自帶事件引擎型：實作 run_engine()，忠實重現既有 v8.5 / SR v2 / v9。"""

    @abstractmethod
    def run_engine(self, data: MarketData,
                   exec_cfg: ExecConfig) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """執行回測，回傳 (trades_df, equity_df)；equity_df 需含 'Equity' 欄。"""
        raise NotImplementedError
