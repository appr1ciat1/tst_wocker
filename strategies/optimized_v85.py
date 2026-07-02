"""
strategies.optimized_v85 — v8.5 約束優化後的兩個正式策略（與 v8.5 明確區分）

mom_guard (GUARD)：v8.5 + 弱勢去風險。graduated regime(floor=0：最弱全出) + breadth + dynamic_topk
                   + 放寬停損 sl3.5；不加碼。最穩健、交易最多。
                   2026-07 起加入 corr_select 相關性分散選股（同 60 日相關>0.7 聚落最多 2 檔），
                   全期 ann/Sharpe/MDD/2022 全面改善（見 GUARD_PARAMS 註解）。
mom_surge (SURGE)：GUARD 的去風險不變 + 分段強勢加碼。只在 0050>MA60/MA20 且 breadth 高、VIX 低時
                   把單筆放大——四段式：弱勢 0% / 強 12.5% / 更強(breadth≥.65,VIX≤20) 14.5% /
                   最強(breadth≥.75,VIX≤15) 17%。追更高報酬，風險與 GUARD 相當甚至更低。

兩者都走 canonical 引擎路徑（atr_df 不傳→引擎內部 ATR；consec_loss_limit=3 預設連損熔斷），
與 ai_report.py / twstk.backtest.runner 一致；訊號重用 MomentumV85.prepare()（同一組 v8.5 評分）。

mom_surge_pro (SURGE PRO)：SURGE 去風險不變 + 更激進分段加碼（VIX 門檻放寬到 28、tier 倍數
                   更高、cap 1.9、hold 25）。四段式：弱 0% / 強 12.5% / 更強(breadth≥.62,VIX≤18)
                   17% / 最強(breadth≥.72,VIX≤15) 18.5%。追最高報酬，代價是 2022 較弱。

驗證（2019-01→2026-06，動態 Top-60，凍結同一份資料；ai_report --eval-start 全期交叉驗證吻合）：
  baseline v8.5 : ann 40.2% / Sharpe 1.43 / MDD -41.1% / Calmar 0.98 / 938 筆
  mom_guard     : ann 51.2% / Sharpe 1.78 / MDD -24.7% / Calmar 2.08 / 1014 筆
  mom_surge     : ann 58.8% / Sharpe 1.86 / MDD -21.5% / Calmar 2.73 / 839 筆
                  PBO 0.34、DSR 0.999、最差年(2022) OOS Sharpe -0.52（baseline -1.33）
  mom_surge_pro : ann 67.1% / Sharpe 2.01 / MDD -22.7% / Calmar 2.96 / 780 筆
                  多元池 PBO 0.086、DSR 1.000、2022 OOS Sharpe -1.13（較 SURGE 弱、換 +8pp 年化）
"""

from typing import Tuple

import pandas as pd

from strategies.base import EngineStrategy, MarketData, ExecConfig
from strategies.registry import register
from strategies.momentum_v85 import MomentumV85
from strategy.event_backtest import EventDrivenBacktester


# ── 驗證過的參數（單一真實來源；ai_report 每日 page 的 flags 應與此一致）──
GUARD_PARAMS = dict(
    sl_atr=3.5, regime_graduated=True, breadth_regime=True, regime_floor=0.0,
    dynamic_topk=True, dynamic_gap_filter=False, position_size=0.10,
    # 建議A（效率前緣/共變異數觀點）：greedy 相關性選股——候選與(持倉∪已選)60日相關
    # >0.7 的檔數達 2 即跳過（同聚落最多 2 檔）。2026-07 驗證：ann 43.2→51.6%、
    # Sharpe 1.54→1.78、MDD -34.2→-26.8%、2022 -14.9→-10.3%，鄰域(0.65-0.75/40-80)全穩健。
    # 注：cap1(嚴禁同群)與 SURGE/SURGE PRO(加碼靠集中聚落)測試皆變差，故只 GUARD 採用。
    corr_select_max=0.70, corr_select_window=60, corr_select_cap=2,
)
SURGE_PARAMS = dict(
    sl_atr=3.5, hold_days=22, regime_graduated=True, breadth_regime=True, regime_floor=0.0,
    dynamic_topk=True, dynamic_gap_filter=True, position_size=0.10,
    regime_sizing=True, strong_regime_mult=1.25, strong_breadth_min=0.55, strong_vix_max=25.0,
    max_regime_scale=1.7, strong_tiers=[(0.65, 20.0, 1.45), (0.75, 15.0, 1.75)],
)
# SURGE PRO：去風險不變，但「分段加碼」更激進（放寬 VIX 門檻 28、更高倍數、cap 1.9、hold 25）。
# 追更高報酬（全期年化 67% vs SURGE 59%），代價是 2022 那年較弱（OOS Sharpe -1.13 vs -0.52）。
SURGE_PRO_PARAMS = dict(
    sl_atr=3.5, hold_days=25, regime_graduated=True, breadth_regime=True, regime_floor=0.0,
    dynamic_topk=True, dynamic_gap_filter=True, position_size=0.10,
    regime_sizing=True, strong_regime_mult=1.25, strong_breadth_min=0.55, strong_vix_max=28.0,
    max_regime_scale=1.9, strong_tiers=[(0.62, 18.0, 1.7), (0.72, 15.0, 1.85)],
)


def _build_engine(p: dict, exec_cfg: ExecConfig) -> EventDrivenBacktester:
    """把參數 dict 映射成 EventDrivenBacktester（其餘風控走引擎預設，含 consec_loss_limit=3）。"""
    return EventDrivenBacktester(
        tp_sl_mode='atr',
        tp_atr_mult=p.get('tp_atr', 4.0), sl_atr_mult=p.get('sl_atr', 3.0),
        max_hold_days=p.get('hold_days', 20), gap_filter_atr=p.get('gap_filter_atr', 1.5),
        position_size=p.get('position_size', 0.10),
        initial_capital=exec_cfg.initial_capital, buy_cost=exec_cfg.buy_cost,
        sell_cost=exec_cfg.sell_cost, slippage=exec_cfg.slippage,
        hybrid_tiered=False,
        regime_filter=True,
        regime_graduated=p.get('regime_graduated', False), regime_floor=p.get('regime_floor', 0.30),
        breadth_regime=p.get('breadth_regime', False),
        dynamic_topk=p.get('dynamic_topk', False), dynamic_gap_filter=p.get('dynamic_gap_filter', False),
        regime_sizing=p.get('regime_sizing', False),
        strong_regime_mult=p.get('strong_regime_mult', 1.25),
        strong_breadth_min=p.get('strong_breadth_min', 0.55),
        strong_vix_max=p.get('strong_vix_max', 20.0),
        max_regime_scale=p.get('max_regime_scale', 1.50),
        strong_tiers=p.get('strong_tiers'),
        corr_select_max=p.get('corr_select_max', 0.0),
        corr_select_window=p.get('corr_select_window', 60),
        corr_select_cap=p.get('corr_select_cap', 1),
    )


def _run(p: dict, data: MarketData, exec_cfg: ExecConfig) -> Tuple[pd.DataFrame, pd.DataFrame]:
    sig = MomentumV85().prepare(data)   # 同一組 v8.5 訊號（Mom×3 + Trend×1）
    bt = _build_engine(p, exec_cfg)
    # atr_df 不傳 → 引擎內部 _compute_atr；regime_sizing 開時引擎內部自抓 VIX。
    return bt.run(
        sig.total_score, data.close, data.open, data.high, data.low, sig.ma_long,
        top_k=exec_cfg.top_k, threshold=exec_cfg.threshold,
        market_close=data.market_close, vol_df=data.volume, universe_mask=data.universe_mask,
    )


@register("mom_guard")
class MomGuard(EngineStrategy):
    description = "GUARD｜v8.5+弱勢去風險+相關性分散選股(60日corr>0.7聚落最多2檔)，不加碼，最穩健"

    def run_engine(self, data: MarketData, exec_cfg: ExecConfig):
        return _run({**GUARD_PARAMS, **self.params}, data, exec_cfg)


@register("mom_surge")
class MomSurge(EngineStrategy):
    description = "SURGE｜GUARD去風險不變+分段強勢加碼(弱0%/強12.5/更強14.5/最強17%)，追更高報酬風險相當"

    def run_engine(self, data: MarketData, exec_cfg: ExecConfig):
        return _run({**SURGE_PARAMS, **self.params}, data, exec_cfg)


@register("mom_surge_pro")
class MomSurgePro(EngineStrategy):
    description = "SURGE PRO｜SURGE去風險不變+更激進分段加碼(弱0%/強12.5/更強17%/最強18.5%)，追最高報酬(67%)，2022較弱"

    def run_engine(self, data: MarketData, exec_cfg: ExecConfig):
        return _run({**SURGE_PRO_PARAMS, **self.params}, data, exec_cfg)
