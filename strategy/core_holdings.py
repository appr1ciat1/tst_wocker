"""
Core Holdings 篩選與維護模組 (Core-Satellite Framework)

提供客觀化多因子篩選，選出 3-5 檔高信心「Core」標的：
- 高流動性（20/60 日平均成交額 Top 段）
- 機構持股穩定性（三大法人持股變化低波動 + 高持股比重）
- 結構性競爭優勢（全球供應鏈關鍵地位，預設權重龍頭）
- 與 Satellite 部位低相關性（簡化為與大盤 beta 適中或手動控制）
- 名額上限 + 每季定期更新

所有篩選決策寫入 experiment_registry 供審計。
與現有 regime / vol target 作為 overlay 相容。

使用：
    from strategy.core_holdings import CoreHoldingsManager
    mgr = CoreHoldingsManager()
    cores = mgr.select_core(close_df, vol_df, asof_date=...)
    mgr.log_selection(registry, cores, scores, hypothesis="Q2 2026 core refresh")
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from research.experiment_registry import ExperimentRegistry, make_experiment_id


# 結構性龍頭 / 關鍵供應鏈標的（可擴充，附簡要 rationale）
# 這些給予結構優勢加分；定期人工覆核但程式以分數為主
STRUCTURAL_LEADERS: Dict[str, str] = {
    '2330': 'TSMC - 全球晶圓代工龍頭，AI/先進製程關鍵供應鏈',
    '2454': 'MediaTek - 手機/車用/邊緣AI SoC 領導廠商',
    '2308': '台達電 - 電源管理/散熱/電動車關鍵零組件',
    '2317': '鴻海 - 全球電子代工龍頭，AI伺服器/電動車布局',
    '2412': '中華電 - 電信基礎設施龍頭，穩定現金流',
    '2881': '富邦金 - 金控龍頭，壽險/銀行/證券綜合優勢',
    '3008': '大立光 - 手機鏡頭龍頭，光學供應鏈關鍵',
}

# 預設 Core 上限
DEFAULT_CORE_CAP = 5
MIN_CORE = 2
MAX_CORE = 5

# 流動性門檻：至少在 20d 平均成交額 Universe Top-80 內才考慮 Core
LIQ_TOP_N_FOR_CORE = 80


class CoreHoldingsManager:
    """
    Core 持股篩選與生命週期管理。
    支援每季自動候選重算 + 手動 override + registry audit。
    """

    def __init__(
        self,
        core_cap: int = DEFAULT_CORE_CAP,
        structural_boost: float = 0.25,
        liq_weight: float = 0.40,
        inst_weight: float = 0.35,
        struct_weight: float = 0.25,
        registry_path: str = "artifacts/experiments.sqlite",
    ):
        self.core_cap = max(MIN_CORE, min(core_cap, MAX_CORE))
        self.structural_boost = structural_boost
        self.liq_weight = liq_weight
        self.inst_weight = inst_weight
        self.struct_weight = struct_weight
        self.registry = ExperimentRegistry(registry_path)
        self._last_selection: Optional[List[str]] = None
        self._last_asof: Optional[str] = None

    def _compute_liquidity_score(
        self, close_df: pd.DataFrame, vol_df: pd.DataFrame, asof: pd.Timestamp
    ) -> pd.Series:
        """20d + 60d 平均成交額排名 → 0~1 分數（越高越好）。"""
        if close_df.empty or vol_df.empty:
            return pd.Series(dtype=float)

        turnover_20 = (close_df * vol_df).rolling(20).mean()
        turnover_60 = (close_df * vol_df).rolling(60).mean()

        # 取 asof 當日（或最後可用）
        if asof not in turnover_20.index:
            asof = turnover_20.index[turnover_20.index <= asof][-1] if len(turnover_20) > 0 else turnover_20.index[-1]

        t20 = turnover_20.loc[asof].dropna()
        t60 = turnover_60.loc[asof].dropna() if asof in turnover_60.index else t20

        # 綜合流動性（調和平均或簡單平均）
        liq_raw = (t20 + t60) / 2.0
        liq_raw = liq_raw[liq_raw > 0]

        # 排名正規化到 [0,1]，Top 越高分
        rank = liq_raw.rank(ascending=False, pct=True)
        # 只保留前 LIQ_TOP_N_FOR_CORE 作為候選池
        mask_top = rank <= (LIQ_TOP_N_FOR_CORE / len(rank)) if len(rank) > 0 else pd.Series(False)
        score = (rank * mask_top.astype(float)).clip(lower=0.0)
        return score

    def _compute_inst_stability_score(
        self, tickers: List[str], close_df: pd.DataFrame
    ) -> pd.Series:
        """
        利用 institutional_flow 估計機構穩定性。
        優先： three_inst_ratio 高 + change_20 波動低。
        無數據時給中性 0.5。
        """
        try:
            from strategy.institutional_flow import build_inst_flow_df
            res = build_inst_flow_df(tickers, close_df, verbose=False)
            inst_df = res[0] if isinstance(res, (list, tuple)) and len(res) > 0 else res
        except Exception:
            inst_df = None

        scores = {}
        for t in tickers:
            base = 0.5
            if inst_df is not None and hasattr(inst_df, 'columns') and t in inst_df.columns:
                try:
                    s = inst_df[t].dropna()
                    if len(s) >= 10:
                        # 持股比重平均高較好
                        avg_ratio = s.mean()
                        # 變化率穩定（低 std of diff）
                        chg = s.diff().std()
                        stab = 1.0 / (1.0 + (chg if pd.notna(chg) else 0.5))
                        # 綜合：比重權重 0.6 + 穩定 0.4
                        base = 0.6 * min(1.0, avg_ratio / 60.0) + 0.4 * min(1.0, stab)
                except Exception:
                    pass
            scores[t] = float(np.clip(base, 0.0, 1.0))
        return pd.Series(scores)

    def _compute_structural_score(self, tickers: List[str]) -> pd.Series:
        """結構性優勢分數：命中 STRUCTURAL_LEADERS 給 boost，否則 0.3 baseline。"""
        scores = {}
        for t in tickers:
            if t in STRUCTURAL_LEADERS:
                scores[t] = 1.0
            else:
                # 非核心結構但仍是高流動權值 → 給 baseline
                scores[t] = 0.3
        return pd.Series(scores)

    def select_core(
        self,
        close_df: pd.DataFrame,
        vol_df: pd.DataFrame,
        asof_date: Optional[str] = None,
        forced: Optional[List[str]] = None,
        min_liq_pct: float = 0.15,
    ) -> Tuple[List[str], Dict[str, float], Dict[str, Any]]:
        """
        執行 Core 篩選。返回 (core_tickers, score_dict, metadata)

        步驟：
        1. 計算各因子分數
        2. 加權總分 = liq*w1 + inst*w2 + struct*w3
        3. 取 Top-N（上限 self.core_cap），並過濾最低流動性
        4. 若 forced 提供則優先合併（仍受 cap 限制）
        """
        if close_df.empty:
            return [], {}, {"error": "no data"}

        if asof_date is None:
            asof = close_df.index[-1]
        else:
            asof = pd.Timestamp(asof_date)

        # 候選池：所有有數據的 ticker
        candidates = [str(c) for c in close_df.columns if str(c).isdigit() or len(str(c)) <= 5]

        # 1. Liquidity
        liq_score = self._compute_liquidity_score(close_df, vol_df, asof)
        liq_score = liq_score.reindex(candidates).fillna(0.0)

        # 2. Inst
        inst_score = self._compute_inst_stability_score(candidates, close_df)

        # 3. Structural
        struct_score = self._compute_structural_score(candidates)

        # 總分（加權 + structural boost 額外加分）
        total = (
            self.liq_weight * liq_score +
            self.inst_weight * inst_score +
            self.struct_weight * struct_score
        )
        # 給予 structural leaders 額外 boost（不超過 1）
        total = total + self.structural_boost * struct_score
        total = total.clip(upper=1.0)

        # 過濾流動性不足者（liq_score 必須 > min_liq_pct 對應排名）
        liq_rank = liq_score.rank(ascending=False, pct=True)
        liquid_mask = (liq_rank <= 0.25) | (liq_score > min_liq_pct)  # 約 Top-25% 或絕對值
        total = total.where(liquid_mask, 0.0)

        # 強制納入（例如使用者指定 TSMC 一定要）
        if forced:
            for t in forced:
                if t in total.index:
                    total[t] = max(total[t], 0.95)  # 幾乎保證入選

        # 選 Top core_cap
        ranked = total.sort_values(ascending=False)
        selected = [t for t, s in ranked.head(self.core_cap).items() if s > 0.0]

        # 至少保留 2 檔（若資料不足則放寬）
        if len(selected) < MIN_CORE and len(ranked) >= MIN_CORE:
            selected = list(ranked.head(MIN_CORE).index)

        score_dict = {t: round(float(total.get(t, 0.0)), 4) for t in selected}
        meta = {
            "asof": str(asof.date()) if hasattr(asof, 'date') else str(asof),
            "n_candidates": len(candidates),
            "core_cap": self.core_cap,
            "forced": forced or [],
            "weights": {
                "liq": self.liq_weight,
                "inst": self.inst_weight,
                "struct": self.struct_weight,
                "struct_boost": self.structural_boost,
            },
            "structural_leaders_used": [t for t in selected if t in STRUCTURAL_LEADERS],
        }

        self._last_selection = selected
        self._last_asof = meta["asof"]
        return selected, score_dict, meta

    def is_quarterly_update_due(self, last_update: Optional[str] = None) -> bool:
        """簡單季度判斷：若 last_update 與今天跨季則需更新。"""
        today = datetime.now()
        q_today = (today.year, (today.month - 1) // 3 + 1)
        if not last_update:
            return True
        try:
            lu = datetime.fromisoformat(last_update[:10])
            q_last = (lu.year, (lu.month - 1) // 3 + 1)
            return q_today != q_last
        except Exception:
            return True

    def log_selection(
        self,
        cores: List[str],
        scores: Dict[str, float],
        meta: Dict[str, Any],
        hypothesis: str = "Core holdings quarterly refresh (Hybrid Tiered Risk Budgeting)",
        decision: str = "selected",
    ) -> str:
        """將本次 Core 選擇寫入 experiment registry。"""
        exp_id = make_experiment_id("core")
        payload = {
            "cores": cores,
            "scores": scores,
            "meta": meta,
            "rationale": {t: STRUCTURAL_LEADERS.get(t, "liquidity+inst selected") for t in cores},
        }
        self.registry.record_experiment(
            experiment_id=exp_id,
            source="core_holdings.select_core",
            strategy_version="v9-hybrid-tiered",
            hypothesis=hypothesis,
            parameter_space={"core_cap": self.core_cap, "weights": meta.get("weights")},
            number_of_trials=1,
            in_sample_period=meta.get("asof"),
            metrics={"selected_count": len(cores), "avg_score": float(np.mean(list(scores.values()))) if scores else 0.0},
            decision=decision,
            notes=json.dumps(payload, ensure_ascii=False),
            trials=[{
                "trial_id": "core_select_1",
                "parameters": {"asof": meta.get("asof"), "forced": meta.get("forced")},
                "metrics": {"cores": cores},
                "decision": decision,
            }],
        )
        return exp_id

    def get_structural_rationale(self, ticker: str) -> str:
        return STRUCTURAL_LEADERS.get(ticker, "objective multi-factor selection (liq+inst+struct)")


def get_default_core() -> List[str]:
    """方便外部直接取最常見的預設 Core（例如 TSMC 為 anchor）。"""
    return ['2330']  # 至少包含龍頭，其餘由 select_core 動態補


if __name__ == "__main__":
    # 簡單自測
    print("CoreHoldingsManager module loaded. Use via import in ai_report / paper / backtest.")
    print("Default structural leaders:", list(STRUCTURAL_LEADERS.keys())[:3], "...")