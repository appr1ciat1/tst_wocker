"""
strategies.ew_momentum — 範例：全新「目標權重型」策略

示範如何不走評分型(SignalStrategy)，直接實作通用契約 target_weights()。
這是給你未來寫各種全新策略的最小範本。

邏輯（刻意簡單，僅作示範）：
  每日取「過去 N 日報酬」最高的 K 檔，等權持有；其餘出場。
"""

import pandas as pd

from strategies.base import WeightStrategy, MarketData
from strategies.registry import register


@register("ew_momentum")
class EqualWeightMomentum(WeightStrategy):
    description = "範例：過去 N 日報酬 Top-K 等權（目標權重型）"

    def __init__(self, lookback: int = 60, top_k: int = 5, **params):
        super().__init__(lookback=lookback, top_k=top_k, **params)
        self.lookback = lookback
        self.top_k = top_k

    def target_weights(self, data: MarketData) -> pd.DataFrame:
        close = data.close
        # 過去 lookback 日報酬
        momentum = close.pct_change(self.lookback)

        # 只在流動性池內挑（若有）
        if data.universe_mask is not None:
            momentum = momentum.where(data.universe_mask.reindex_like(momentum).fillna(False))

        ranks = momentum.rank(axis=1, ascending=False)
        selected = (ranks <= self.top_k) & momentum.notna()

        w = selected.astype(float)
        row_sum = w.sum(axis=1).replace(0, pd.NA)
        w = w.div(row_sum, axis=0).fillna(0.0)
        return w
