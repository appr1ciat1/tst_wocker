"""
Portfolio Volatility Targeting + Tiered Core-Satellite Risk Budgeting (v9 Hybrid)

資金輪動主軸（非對稱設計：壓力區防守溫和、冷卻區進攻積極）：
- 平常（fvol ≤ trigger）：Core / Satellite 皆 1.0×，等同 v8.5 滿倉。
- 高波動（trigger < fvol ≤ crisis）：僅縮減 Satellite 新倉 sizing（最低 0.85×）、
  Core 略增（最高 1.5×）；不主動賣出 Satellite 持倉。
- 極端危機（fvol > crisis）：暫停 Satellite 新倉 + 更嚴回撤暫停。
- 波動回落（fvol 跌回 trigger 以下）：主動了結 Core α 獲利（85%）、
  冷卻 16 日 Satellite 全體 2.1× 加碼——v9 主要 alpha 來源。

其餘：EWMA 預測組合 vol、與 regime filter（0050/MA + Breadth）overlay 相容。
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


TARGET_ANN_VOL_DEFAULT = 0.15   # 15% 監控目標
VOL_BAND = (0.12, 0.15)         # 舒適區間（報告用）
ROTATION_TRIGGER_VOL = 0.22     # 預設輪動門檻（平常等同 v8.5 滿倉）
CRISIS_VOL = 0.30               # 極端波動：暫停 Sat 新倉 + 嚴格回撤暫停
DD_PAUSE_NORMAL = 0.10          # 平常回撤暫停門檻（同 v8.5）
DD_PAUSE_STRESS = 0.09          # 壓力波動時略收緊
DD_PAUSE_CRISIS = 0.07          # 極端波動時嚴格

# 壓力區輪動曲線（trigger < fvol ≤ crisis）：溫和縮 Sat / 略增 Core
STRESS_SAT_FLOOR_DEFAULT = 0.85
STRESS_CORE_CEILING_DEFAULT = 1.50

# 高波動升破門檻：僅調整新倉 sizing，不主動賣 Sat（回測顯示強制減碼傷害報酬）
SAT_ALPHA_TRIM_FRAC_DEFAULT = 0.0
SAT_ALPHA_TRIM_MIN_PNL_DEFAULT = 0.02

# 波動回落：主動賣 Core α 獲利 → 資金輪動回 Satellite 動能（v9 主要 alpha 來源）
COOLING_DAYS_DEFAULT = 16
COOLING_SAT_BOOST_DEFAULT = 2.10
COOLING_CORE_BOOST_DEFAULT = 0.50
CORE_ALPHA_TRIM_FRAC_DEFAULT = 0.85
CORE_ALPHA_TRIM_MIN_PNL_DEFAULT = 0.005

# V3 最佳參數組（full_sweep Stage 3 + Stage 4 驗證：ann +79% / Sharpe 2.46 / MDD -18.8%）
V3_PRODUCTION_LABEL = 'V3 最佳參數組'


def v3_production_kwargs(rotation_trigger=None, crisis_vol=None):
    """EventDrivenBacktester tiered kwargs for validated V3 production defaults."""
    return {
        'rotation_trigger_vol': (
            rotation_trigger if rotation_trigger is not None else ROTATION_TRIGGER_VOL
        ),
        'crisis_vol': crisis_vol if crisis_vol is not None else CRISIS_VOL,
        'stress_sat_floor': STRESS_SAT_FLOOR_DEFAULT,
        'stress_core_ceiling': STRESS_CORE_CEILING_DEFAULT,
        'cooling_sat_boost': COOLING_SAT_BOOST_DEFAULT,
        'cooling_core_boost': COOLING_CORE_BOOST_DEFAULT,
        'sat_alpha_trim_frac': SAT_ALPHA_TRIM_FRAC_DEFAULT,
        'sat_alpha_trim_min_pnl': SAT_ALPHA_TRIM_MIN_PNL_DEFAULT,
        'core_alpha_trim_frac': CORE_ALPHA_TRIM_FRAC_DEFAULT,
        'core_alpha_trim_min_pnl': CORE_ALPHA_TRIM_MIN_PNL_DEFAULT,
        'cooling_days': COOLING_DAYS_DEFAULT,
    }


def v3_vol_target_config() -> VolTargetConfig:
    """VolTargetConfig for paper tracker / replay — identical tiered params to V3 backtest."""
    kw = v3_production_kwargs()
    return VolTargetConfig(
        target_ann_vol=TARGET_ANN_VOL_DEFAULT,
        rotation_trigger_vol=kw['rotation_trigger_vol'],
        crisis_vol=kw['crisis_vol'],
        cooling_days=kw['cooling_days'],
        cooling_sat_boost=kw['cooling_sat_boost'],
        cooling_core_boost=kw['cooling_core_boost'],
        stress_sat_floor=kw['stress_sat_floor'],
        stress_core_ceiling=kw['stress_core_ceiling'],
        sat_alpha_trim_frac=kw['sat_alpha_trim_frac'],
        sat_alpha_trim_min_pnl=kw['sat_alpha_trim_min_pnl'],
        core_alpha_trim_frac=kw['core_alpha_trim_frac'],
        core_alpha_trim_min_pnl=kw['core_alpha_trim_min_pnl'],
    )


def build_v3_production_backtester(args):
    """Construct EventDrivenBacktester identical to run_full_sweep / compare_v85_v9."""
    from strategy.event_backtest import EventDrivenBacktester

    return EventDrivenBacktester(
        tp_sl_mode='atr',
        tp_atr_mult=args.tp_atr,
        sl_atr_mult=args.sl_atr,
        max_hold_days=args.hold_days,
        initial_capital=args.capital,
        position_size=args.position_size,
        regime_filter=True,
        regime_graduated=True,
        regime_floor=args.regime_floor,
        gap_filter_atr=args.gap_filter,
        breadth_regime=True,
        hybrid_tiered=True,
        core_tickers=['2330', '2454', '2308', '2317', '3008'],
        target_ann_vol=0.15,
        buy_cost=getattr(args, 'buy_cost', 0.001425),
        sell_cost=getattr(args, 'sell_cost', 0.004425),
        corr_filter=0.0,
        gap_aware_sizing=False,
        slippage=0.0,
        **v3_production_kwargs(
            getattr(args, 'rotation_trigger', None),
            getattr(args, 'crisis_vol', None),
        ),
    )


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
    rotation_trigger_vol: float = ROTATION_TRIGGER_VOL
    crisis_vol: float = CRISIS_VOL
    cooling_days: int = COOLING_DAYS_DEFAULT
    cooling_sat_boost: float = COOLING_SAT_BOOST_DEFAULT
    cooling_core_boost: float = COOLING_CORE_BOOST_DEFAULT
    stress_sat_floor: float = STRESS_SAT_FLOOR_DEFAULT
    stress_core_ceiling: float = STRESS_CORE_CEILING_DEFAULT
    sat_alpha_trim_frac: float = SAT_ALPHA_TRIM_FRAC_DEFAULT
    sat_alpha_trim_min_pnl: float = SAT_ALPHA_TRIM_MIN_PNL_DEFAULT
    core_alpha_trim_frac: float = CORE_ALPHA_TRIM_FRAC_DEFAULT
    core_alpha_trim_min_pnl: float = CORE_ALPHA_TRIM_MIN_PNL_DEFAULT
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
        self._prev_forecast_vol: Optional[float] = None

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
        """超過輪動觸發門檻的程度（>0 才啟動資金輪動）。"""
        trigger = self.cfg.rotation_trigger_vol
        if forecast_ann_vol <= trigger:
            return 0.0
        return (forecast_ann_vol - trigger) / max(trigger, 1e-6)

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
        - Core / Satellite trade scale 永遠 = 1.0（選股後買滿）
        - fvol ≤ rotation_trigger：rotation_boost = 1.0（等同 v8.5 滿倉）
        - fvol > rotation_trigger：僅透過 capital_rotation 調整（Core↑ / Sat↓）
        - core_effective / sat_effective 僅供帳本預算參考，不可乘在單筆倉位
        """
        fvol = forecast_ann_vol if forecast_ann_vol is not None else (self._last_forecast or self.cfg.target_ann_vol)
        over = self.compute_overage(fvol)
        self._last_over = over

        target = self.cfg.target_ann_vol
        band_high = self.cfg.max_ann_vol
        if fvol <= band_high:
            overall = 1.0
        else:
            overall = min(1.0, max(0.10, band_high / max(fvol, 1e-4)))

        # 基礎曝險（可由 caller 傳入當前 book 目標）
        bc = base_core_gross if base_core_gross is not None else self.cfg.core_base_gross
        bs = base_sat_gross if base_sat_gross is not None else self.cfg.sat_base_gross

        # Core 緩和衰減：1 - core_decay * over ，但保留 core_floor
        core_mult = max(self.cfg.core_floor, 1.0 - self.cfg.core_decay * over)
        # Sat 積極衰減
        sat_mult = max(self.cfg.sat_floor, 1.0 - self.cfg.sat_decay * over)

        core_eff = bc * core_mult * overall
        sat_eff = bs * sat_mult * overall
        core_trade_scale = 1.0
        sat_trade_scale = 1.0

        scales = {
            "overall": round(float(overall), 4),
            "core_mult": round(float(core_mult), 4),
            "sat_mult": round(float(sat_mult), 4),
            "core_trade_scale": round(float(core_trade_scale), 4),
            "sat_trade_scale": round(float(sat_trade_scale), 4),
            "core_effective": round(float(core_eff), 4),
            "sat_effective": round(float(sat_eff), 4),
            "forecast_ann_vol": round(float(fvol), 4),
            "target_ann_vol": round(float(target), 4),
            "over": round(float(over), 4),
        }
        rotation = self.capital_rotation(fvol, self._prev_forecast_vol)
        scales.update(rotation)
        self._prev_forecast_vol = float(fvol)
        self._last_scales = scales
        return scales

    def capital_rotation(
        self,
        forecast_ann_vol: float,
        prev_forecast: Optional[float] = None,
    ) -> Dict[str, float]:
        """
        v9 資金輪動曲線：
        - fvol ≤ trigger：Core/Sat = 1.0（等同 v8.5）
        - trigger < fvol ≤ crisis：Sat↓ Core↑（資金從 Satellite 避險至 Core α）
        - fvol > crisis：暫停 Sat 新倉 + 更嚴回撤暫停
        - 波動升破 trigger：主動減碼獲利 Satellite
        - 波動回落至 trigger 以下：Core α 獲利了結，資金回流 Satellite
        """
        trigger = self.cfg.rotation_trigger_vol
        crisis = self.cfg.crisis_vol

        core_rotation_boost = 1.0
        sat_rotation_boost = 1.0
        rotate_sat_profits_to_core = 0.0
        rotate_core_profits_to_sat = 0.0
        sat_entry_freeze = 0.0
        vol_regime = 'normal'
        dd_pause_pct = DD_PAUSE_NORMAL
        stress_transition = False
        cooling_transition = False
        cooling_active = False

        if forecast_ann_vol > crisis:
            vol_regime = 'crisis'
            excess = min(1.5, (forecast_ann_vol - crisis) / max(crisis, 1e-6))
            sat_rotation_boost = max(0.40, 0.55 - 0.10 * min(excess, 1.0))
            core_rotation_boost = min(1.50, 1.20 + 0.20 * min(excess, 1.0))
            sat_entry_freeze = 1.0
            dd_pause_pct = DD_PAUSE_CRISIS
        elif forecast_ann_vol > trigger:
            vol_regime = 'stress'
            excess = min(1.5, (forecast_ann_vol - trigger) / max(trigger, 1e-6))
            sat_floor = self.cfg.stress_sat_floor
            core_ceil = self.cfg.stress_core_ceiling
            sat_span = max(0.05, 1.0 - sat_floor)
            core_span = max(0.05, core_ceil - 1.0)
            core_rotation_boost = min(core_ceil, 1.0 + core_span * excess / 1.5)
            sat_rotation_boost = max(sat_floor, 1.0 - sat_span * excess / 1.5)
            dd_pause_pct = DD_PAUSE_STRESS

        cooling_days_left = getattr(self, '_cooling_days_left', 0)
        # 僅在「平常 → 高波動」首次升破門檻時減碼 Sat；冷卻視窗內不重複觸發
        if (prev_forecast is not None
                and prev_forecast <= trigger
                and forecast_ann_vol > trigger
                and cooling_days_left <= 0):
            stress_transition = True
            rotate_sat_profits_to_core = 1.0

        if prev_forecast is not None and prev_forecast > trigger and forecast_ann_vol <= trigger:
            cooling_transition = True
            rotate_core_profits_to_sat = 1.0
            vol_regime = 'cooling'

        if getattr(self, '_cooling_days_left', 0) > 0 or cooling_transition:
            cooling_active = True
            vol_regime = 'cooling'
            sat_rotation_boost = max(sat_rotation_boost, self.cfg.cooling_sat_boost)
            core_rotation_boost = min(core_rotation_boost, self.cfg.cooling_core_boost)
            sat_entry_freeze = 0.0

        return {
            'vol_regime': vol_regime,
            'core_rotation_boost': round(float(core_rotation_boost), 4),
            'sat_rotation_boost': round(float(sat_rotation_boost), 4),
            'rotate_sat_profits_to_core': round(float(rotate_sat_profits_to_core), 4),
            'rotate_core_profits_to_sat': round(float(rotate_core_profits_to_sat), 4),
            'sat_entry_freeze': round(float(sat_entry_freeze), 4),
            'dd_pause_pct': round(float(dd_pause_pct), 4),
            'stress_transition': float(stress_transition),
            'cooling_transition': float(cooling_transition),
            'cooling_active': float(cooling_active),
            'sat_alpha_trim_frac': round(float(self.cfg.sat_alpha_trim_frac), 4),
            'sat_alpha_trim_min_pnl': round(float(self.cfg.sat_alpha_trim_min_pnl), 4),
            'core_alpha_trim_frac': round(float(self.cfg.core_alpha_trim_frac), 4),
            'core_alpha_trim_min_pnl': round(float(self.cfg.core_alpha_trim_min_pnl), 4),
        }

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

        c_mult = scales.get("core_trade_scale", 1.0)
        s_mult = scales.get("sat_trade_scale", 1.0)

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