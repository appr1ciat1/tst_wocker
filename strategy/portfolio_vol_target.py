"""
Portfolio Volatility Targeting + Tiered Core-Satellite Risk Budgeting (v9 Hybrid)

核心設計：
- 最上層 Portfolio Volatility Targeting：每日 EWMA（或可選 GARCH）預測整個組合 realized/forecast vol。
- 目標年化波動率 8–12%（可配置 band）。
- 當預測 vol 超過目標 → 輸出總 scale factor 並動態調整 gross exposure。
- Core（高信心）：較高基礎曝險（建議總曝險貢獻 20-30%）、較緩和 scale 調整、較寬鬆尾部風險容忍。
- Satellite（v8.5 + SR v2 訊號產生之衛星部位）：嚴格受 vol 目標約束，波動率上升時優先且較大幅度 scale down。
- Tiered Scaling：vol 上升時 Sat 承受較大調整係數；Core 設較小衰減或 floor。

與現有 regime filter（0050/MA + Breadth、US Macro）作為 overlay 相容。
Core 與 Satellite 分別維護 equity curve，合併後計算 portfolio vol。

每日流程建議：
  signals (mom + sr) → tag core/sat → sizing with base exposure →
  forecast combined vol → tiered_scales → risk check → execute / log to registry

無額外重依賴（EWMA 為主；GARCH 需 `arch` 時 graceful fallback）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from arch import arch_model  # optional
    HAS_ARCH = True
except Exception:
    HAS_ARCH = False


TARGET_ANN_VOL_DEFAULT = 0.10  # 10%
VOL_BAND = (0.08, 0.12)        # 建議區間

# 預設 tiered 參數（可 override）
# 當 forecast_vol > target 時的衰減敏感度：sat > core
DEFAULT_CORE_DECAY = 0.35
DEFAULT_SAT_DECAY = 0.85
DEFAULT_CORE_FLOOR = 0.55   # Core 最低保留曝險（保護高信心 alpha）
DEFAULT_SAT_FLOOR = 0.15    # Sat 可大幅降到很低


@dataclass
class VolTargetConfig:
    target_ann_vol: float = TARGET_ANN_VOL_DEFAULT
    min_ann_vol: float = VOL_BAND[0]
    max_ann_vol: float = VOL_BAND[1]
    ewma_lambda: float = 0.94          # RiskMetrics 風格
    vol_lookback: int = 60             # 用最近 N 日報酬預測
    core_decay: float = DEFAULT_CORE_DECAY
    sat_decay: float = DEFAULT_SAT_DECAY
    core_floor: float = DEFAULT_CORE_FLOOR
    sat_floor: float = DEFAULT_SAT_FLOOR
    core_base_gross: float = 0.25      # Core 基礎總曝險目標（建議 20-30%）
    sat_base_gross: float = 0.75       # Sat 基礎（其餘）
    use_garch: bool = False            # 僅在 arch 可用時有效
    garch_p: int = 1
    garch_q: int = 1


class PortfolioVolatilityTarget:
    """
    組合層級波動率目標 + Core-Satellite 分層風險預算。
    """

    def __init__(self, config: Optional[VolTargetConfig] = None):
        self.cfg = config or VolTargetConfig()
        self._last_forecast: Optional[float] = None
        self._last_over: Optional[float] = None
        self._last_scales: Optional[Dict[str, float]] = None

    # ---------- 波動率預測 ----------

    def ewma_variance(self, returns: pd.Series) -> float:
        """RiskMetrics 風格 EWMA 變異數。"""
        if returns is None or len(returns) < 5:
            return 1e-8
        r = returns.dropna().astype(float)
        if len(r) < 5:
            return float(r.var() if len(r) > 1 else 1e-8)

        lam = self.cfg.ewma_lambda
        # 初始化為無條件變異數
        var = r.var()
        for ret in r.values:
            var = lam * var + (1 - lam) * (ret ** 2)
        return max(var, 1e-12)

    def ewma_ann_vol(self, returns: pd.Series) -> float:
        """年化 EWMA vol。"""
        var = self.ewma_variance(returns)
        return math.sqrt(var * 252.0)

    def garch_ann_vol(self, returns: pd.Series) -> Optional[float]:
        """可選 GARCH(1,1) 預測（需 arch 套件）。失敗回 None。"""
        if not self.cfg.use_garch or not HAS_ARCH or len(returns.dropna()) < 30:
            return None
        try:
            r = (returns.dropna().astype(float) * 100.0)  # arch 常用百分比尺度
            am = arch_model(r, vol='Garch', p=self.cfg.garch_p, q=self.cfg.garch_q,
                            mean='Zero', dist='normal', rescale=False)
            res = am.fit(disp='off', last_obs=None, update_freq=0)
            # 預測下一期 cond var
            fc = res.forecast(horizon=1)
            var_next = fc.variance.iloc[-1, 0] / 10000.0  # 轉回小數尺度
            return float(math.sqrt(var_next * 252.0))
        except Exception:
            return None

    def forecast_portfolio_ann_vol(
        self,
        equity_core: Optional[pd.Series],
        equity_sat: Optional[pd.Series],
        merged_equity: Optional[pd.Series] = None,
    ) -> float:
        """
        由 Core + Satellite equity curve 合併計算組合 realized/forecast vol。
        優先使用 merged_equity（若 caller 已經把兩個 book 權益相加）。
        否則用 (core + sat) 視為總權益計算 pct_change。
        """
        if merged_equity is not None and len(merged_equity) > 5:
            eq = merged_equity.sort_index()
        else:
            eq_c = equity_core.sort_index() if equity_core is not None else None
            eq_s = equity_sat.sort_index() if equity_sat is not None else None
            if eq_c is None and eq_s is None:
                return 0.12  # 保守中性
            if eq_c is None:
                eq = eq_s
            elif eq_s is None:
                eq = eq_c
            else:
                # 對齊後相加（假設兩者 index 為日期，權益單位一致）
                common = eq_c.index.intersection(eq_s.index)
                if len(common) < 5:
                    eq = (eq_c + eq_s).dropna()
                else:
                    eq = (eq_c.reindex(common) + eq_s.reindex(common)).dropna()

        if eq is None or len(eq) < 5:
            return 0.12

        rets = eq.pct_change().dropna().tail(self.cfg.vol_lookback)
        vol = self.garch_ann_vol(rets) if self.cfg.use_garch else None
        if vol is None:
            vol = self.ewma_ann_vol(rets)
        self._last_forecast = float(vol)
        return float(vol)

    # ---------- Tiered Scaling ----------

    def compute_overage(self, forecast_ann_vol: float) -> float:
        """超過目標的程度（>0 表示需要降風險）。"""
        target = self.cfg.target_ann_vol
        if forecast_ann_vol <= target:
            return 0.0
        return (forecast_ann_vol - target) / max(target, 1e-6)

    def tiered_scale_factors(
        self,
        forecast_ann_vol: Optional[float] = None,
        base_core_gross: Optional[float] = None,
        base_sat_gross: Optional[float] = None,
    ) -> Dict[str, float]:
        """
        傳回 tiered 調整後的 scale：
          {
            'overall': overall_gross_scale,   # vol target 產生的總 scale
            'core_mult': core 專用乘數,
            'sat_mult':  sat 專用乘數,
            'core_effective': core_base * core_mult * overall,
            'sat_effective': sat_base * sat_mult * overall,
          }

        設計原則：
        - overall = clamp( target / forecast , 0.1, 1.0 )
        - 再給 Core 較保護的衰減曲線（較小 decay + 較高 floor）
        - Sat 積極降桿（較大 decay + 低 floor）
        """
        fvol = forecast_ann_vol if forecast_ann_vol is not None else (self._last_forecast or self.cfg.target_ann_vol)
        over = self.compute_overage(fvol)
        self._last_over = over

        target = self.cfg.target_ann_vol
        overall = min(1.0, max(0.10, target / max(fvol, 1e-4)))

        # 基礎曝險（可由 caller 傳入當前 book 目標）
        bc = base_core_gross if base_core_gross is not None else self.cfg.core_base_gross
        bs = base_sat_gross if base_sat_gross is not None else self.cfg.sat_base_gross

        # Core 緩和衰減：1 - core_decay * over ，但保留 core_floor
        core_mult = max(self.cfg.core_floor, 1.0 - self.cfg.core_decay * over)
        # Sat 積極衰減
        sat_mult = max(self.cfg.sat_floor, 1.0 - self.cfg.sat_decay * over)

        core_eff = bc * core_mult * overall
        sat_eff = bs * sat_mult * overall

        scales = {
            "overall": round(float(overall), 4),
            "core_mult": round(float(core_mult), 4),
            "sat_mult": round(float(sat_mult), 4),
            "core_effective": round(float(core_eff), 4),
            "sat_effective": round(float(sat_eff), 4),
            "forecast_ann_vol": round(float(fvol), 4),
            "target_ann_vol": round(float(target), 4),
            "over": round(float(over), 4),
        }
        self._last_scales = scales
        return scales

    def apply_to_positions(
        self,
        core_positions: Dict[str, Dict],
        sat_positions: Dict[str, Dict],
        scales: Optional[Dict[str, float]] = None,
    ) -> Tuple[Dict[str, Dict], Dict[str, Dict], Dict[str, float]]:
        """
        將 tiered scale 應用到兩本帳的部位 dict（就地調整建議 size 或 exposure）。
        預期 position 結構為 {'shares': , 'notional': , ...} 或至少含 'size' / 'weight'。
        回傳 (scaled_core_pos, scaled_sat_pos, scales_used)
        """
        if scales is None:
            scales = self._last_scales or self.tiered_scale_factors()

        c_mult = scales.get("core_effective", 1.0)
        s_mult = scales.get("sat_effective", 1.0)

        def _scale_book(book: Dict[str, Dict], mult: float) -> Dict[str, Dict]:
            out = {}
            for t, pos in (book or {}).items():
                p = dict(pos)  # shallow copy
                for k in ("size", "weight", "notional", "target_notional", "risk_dollar"):
                    if k in p and isinstance(p[k], (int, float)):
                        p[k] = p[k] * mult
                # 若有 shares 也同步縮（假設 caller 之後會重算）
                if "shares" in p and isinstance(p["shares"], (int, float)) and "entry_price" in p:
                    p["shares"] = int(round(p["shares"] * mult))
                p["vol_scale"] = round(mult, 4)
                out[t] = p
            return out

        scaled_c = _scale_book(core_positions, c_mult)
        scaled_s = _scale_book(sat_positions, s_mult)
        return scaled_c, scaled_s, scales

    def get_last_state(self) -> Dict[str, Any]:
        return {
            "forecast": self._last_forecast,
            "over": self._last_over,
            "scales": self._last_scales,
            "config": {
                "target": self.cfg.target_ann_vol,
                "core_floor": self.cfg.core_floor,
                "sat_floor": self.cfg.sat_floor,
            },
        }


def compute_merged_equity(
    equity_core: pd.DataFrame | pd.Series,
    equity_sat: pd.DataFrame | pd.Series,
    initial_capital: float = 1_000_000.0,
) -> pd.Series:
    """把兩個 book 的 equity 曲線合併成單一 portfolio equity（用於 vol 預測與報告）。"""
    def _to_series(eq):
        if isinstance(eq, pd.DataFrame):
            if "Equity" in eq.columns:
                return eq["Equity"]
            return eq.iloc[:, 0]
        return eq

    ec = _to_series(equity_core).sort_index()
    es = _to_series(equity_sat).sort_index()
    idx = ec.index.union(es.index)
    merged = (ec.reindex(idx).fillna(method="ffill") + es.reindex(idx).fillna(method="ffill")).dropna()
    # 若起始值偏低，normalize 到 initial（僅供 vol 計算，不影響真實權益）
    if len(merged) > 0 and merged.iloc[0] < initial_capital * 0.1:
        merged = merged * (initial_capital / max(merged.iloc[0], 1.0))
    return merged


if __name__ == "__main__":
    pvt = PortfolioVolatilityTarget()
    print("PortfolioVolatilityTarget ready. target_ann_vol=", pvt.cfg.target_ann_vol)
    print("GARCH available:", HAS_ARCH)
    # 範例 scales
    sc = pvt.tiered_scale_factors(forecast_ann_vol=0.18)
    print("Example scales @18% vol:", sc)