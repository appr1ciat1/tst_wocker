"""
strategies.reversal — 均值回歸(反轉)sleeve

買「近 lookback 日跌最多」的 Top-K 檔(超跌反轉),等權持有 hold 天。
與動能(v8.5/v9)天生負/低相關(動能買贏家、反轉買輸家),
用途是當作分散 sleeve 與 v9 混合,降低組合回撤、提高 Calmar。

驗證(2019–2026):對 v9 相關性僅 ~0.33；v9 80% + 反轉20d 20% 全週期
MDD −30.6%→−24.5%、Calmar 1.52→1.72。單獨報酬低於動能,僅作分散用。
"""
import numpy as np
import pandas as pd

from strategies.base import WeightStrategy, MarketData
from strategies.registry import register


@register("reversal_20d")
class Reversal(WeightStrategy):
    description = "均值回歸(超跌反轉)sleeve，與動能低相關，作分散用"

    def __init__(self, lookback: int = 20, top_k: int = 7, hold: int = 10, **p):
        super().__init__(lookback=lookback, top_k=top_k, hold=hold, **p)
        self.lookback = int(lookback)
        self.top_k = int(top_k)
        self.hold = int(hold)

    def target_weights(self, data: MarketData) -> pd.DataFrame:
        close = data.close
        ret = close.pct_change(self.lookback)
        elig = close.notna() & (close > 0)
        if data.universe_mask is not None:
            elig = elig & data.universe_mask.reindex_like(close).fillna(False)
        masked = ret.where(elig)
        rank = masked.rank(axis=1, ascending=True)        # 跌最多 → rank 小
        sel = (rank <= self.top_k) & masked.notna()
        w = sel.astype(float)
        w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
        if self.hold > 1:                                  # 每 hold 天換一次,降週轉
            w = w.iloc[::self.hold].reindex(w.index, method="ffill").fillna(0.0)
        return w
