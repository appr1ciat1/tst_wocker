"""
strategies.momentum_v9_sbl — v9 + 借券賣出(SBL)空方 tilt

在 v9 Hybrid Tiered(動能 + Core-Satellite + 波動目標)之上，於選股評分加入
「借券賣出餘額」這個法人空方因子的負向 tilt：

    augmented_score = v9_momentum_score + sbl_weight × ( -rank(借券賣出餘額 N 日變化) )

亦即「法人加碼借券放空」的股票降分(避開),「借券回補」的股票相對加分。
IC 分析顯示借券賣出對 20 日前瞻報酬有顯著負相關(法人放空有效),
與散戶「融券」不同。

⚠️ 借券資料目前僅約 1 年歷史 → 因子採保守權重(預設 0.25),需隨資料累積持續驗證。
"""

import numpy as np
import pandas as pd

from strategies.base import MarketData
from strategies.registry import register
from strategies.hybrid_tiered_v9 import HybridTieredV9


@register("momentum_v9_sbl")
class MomentumV9SBL(HybridTieredV9):
    description = "v9 + 借券賣出(SBL)空方 tilt（法人放空避開，權重 0.25）"

    def __init__(self, sbl_weight: float = 0.25, sbl_lookback: int = 20, **params):
        super().__init__(**params)
        self.sbl_weight = float(sbl_weight)
        self.sbl_lookback = int(sbl_lookback)
        # 在底層需求(如 us_signals)之外，再宣告需要借券資料
        self.requires = frozenset(set(self.requires) | {"short_sale"})

    def adjust_score(self, total_score: pd.DataFrame, data: MarketData) -> pd.DataFrame:
        sbl = data.short_sale_df
        if sbl is None or self.sbl_weight == 0:
            if sbl is None:
                print("   ⚠️ 無借券(SBL)資料，momentum_v9_sbl 退化為純 v9")
            return total_score

        sbl = sbl.reindex_like(total_score)
        # 借券賣出餘額 N 日變化（用較乾淨的「變化」而非「水準」，避免規模混淆）
        chg = sbl.pct_change(self.sbl_lookback)
        # 限制在流動性池內做橫斷面排名
        if data.universe_mask is not None:
            chg = chg.where(data.universe_mask.reindex_like(chg).fillna(False))
        rank = chg.rank(axis=1, pct=True)
        # 借券放空增加(高 rank)→ 看空 → 取負；t-1 避免前視
        f_sbl = (-rank).shift(1).reindex_like(total_score).fillna(0.0)
        return total_score + self.sbl_weight * f_sbl
