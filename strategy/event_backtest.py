"""
事件驅動回測引擎 v2 (Event-Driven Backtest Engine)

支援四種出場機制：
1. 區間停利 (Take Profit, TP) — 盤中最高價觸碰目標價即出場
2. 絕對停損 (Stop Loss, SL) — 盤中最低價跌破防守價即砍倉
3. 移動停利 (Trailing Stop) — 從最高點回落超過 ATR 倍數即出場
4. 時間強制出場 (Time Exit) — 持有滿 N 個交易日強制以收盤價平倉

v2 改進：
- Entry 改為 t+1 open（對齊實盤信號流程）
- 支援 Top-K 選股（取代固定 threshold）
- Position sizing 改為 current equity based（非 initial capital）
- 支援 ATR-based TP/SL（波動度自適應）
- 支援 Trailing Stop（移動停利，讓強趨勢自然延伸）
- 加入台股交易成本模型（手續費 + 證交稅）
- 停損優先判定保持不變

核心特色：
- 使用每日 High/Low 進行精確觸價回測（非僅收盤價），貼近實戰
- 停損優先判定（保守原則：同一天同時觸碰 TP 和 SL 時，以 SL 計算）
- 與策略完全解耦：只需給它「分數矩陣 + OHLC 矩陣」就能運行
"""

import pandas as pd
import numpy as np


class EventDrivenBacktester:
    """
    事件驅動回測器 v2。

    Parameters
    ----------
    tp_pct : float
        固定模式停利百分比，例如 0.15 代表 +15%
    sl_pct : float
        固定模式停損百分比，例如 0.08 代表 -8%
    max_hold_days : int
        最大持倉交易日數
    initial_capital : float
        初始模擬資金
    position_size : float
        每次進場佔當前權益的比例（例如 0.10 = 10%）
    tp_sl_mode : str
        'fixed' = 固定百分比 TP/SL, 'atr' = ATR 倍數
    tp_atr_mult : float
        ATR 模式下的停利倍數（預設 3.0）
    sl_atr_mult : float
        ATR 模式下的停損倍數（預設 1.5）
    trailing_stop : bool
        啟用移動停利。啟用時固定 TP 會被停用，改為追蹤最高點回落 sl_atr_mult × ATR 出場。
    trailing_atr_mult : float
        移動停利的 ATR 倍數（預設 2.0，從最高點回落此倍數 ATR 即觸發）
    regime_filter : bool
        啟用大盤過濾（market_close > 60MA 才允許進場）
    gap_filter_atr : float
        跳空過濾（open 偏離 prev_close 超過此倍數 ATR 則跳過），0 = 停用
    volume_confirm : bool
        啟用成交量確認（進場日 volume > 20 日均量）
    blacklist_lookback : int
        動態黑名單回顧筆數（最近 N 筆交易勝率低於 min_wr 則暫時排除），0 = 停用
    blacklist_min_wr : float
        黑名單勝率門檻（預設 0.25 = 25%）
    breakeven_pct : float
        獲利保護觸發門檻（預設 0 = 停用，0.03 = +3%），達到此獲利後將 SL 移至成本價
    slippage : float
        滑價模型（預設 0 = 停用，0.001 = 0.1%），進出場額外執行成本
    vol_parity : bool
        啟用波動率平價 (Volatility Parity) 部位調整，取代固定比例
    mean_reversion : bool
        啟用均值回歸子策略（大盤 < 60MA 時，買入超跌反彈股）
    dynamic_risk : bool
        啟用動態風險預算（根據近 20 日 realized vol 調整 position size）
    futures_hedge : bool
        啟用台指期空單對沖（大盤 < 60MA 時，模擬空單保護部位）
    dd_pause_pct : float
        權益回撤竟日卡門檻（預設 0.10 = 10%），回撤超此比率則暫停新進場 dd_pause_days 天
    dd_pause_days : int
        回撤觸發後暫停新倉天數（預設 5）
    consec_loss_limit : int
        連續停損筆數上限（預設 3），過此則暫停 consec_loss_pause 天
    consec_loss_pause : int
        連續停損後暫停天數（預設 5）
    sector_max_pct : float
        單一板塊最大持倉比例（預設 0.6 = 60%），電子股不超過此比例
    buy_cost : float
        買進手續費率（預設 0.001425 = 0.1425%）
    sell_cost : float
        賣出成本率（手續費 + 證交稅，預設 0.004425 = 0.1425% + 0.3%）
    """

    def __init__(self, tp_pct=0.15, sl_pct=0.08, max_hold_days=20,
                 initial_capital=1_000_000, position_size=0.10,
                 tp_sl_mode='atr', tp_atr_mult=4.0, sl_atr_mult=3.0,
                 trailing_stop=False, trailing_atr_mult=2.0,
                 regime_filter=False, regime_graduated=False,
                 regime_floor=0.30,
                 gap_filter_atr=1.5,
                 volume_confirm=False,
                 blacklist_lookback=0, blacklist_min_wr=0.25,
                 breakeven_pct=0, slippage=0, vol_parity=False,
                 mean_reversion=False, dynamic_risk=False,
                 futures_hedge=False,
                 dd_pause_pct=0.10, dd_pause_days=5,
                 consec_loss_limit=3, consec_loss_pause=5,
                 sector_max_pct=0.75,
                 corr_filter=0,
                 corr_select_max=0.0,
                 corr_select_window=60,
                 corr_select_cap=1,
                 max_portfolio_heat=1.0,
                 rank_weighted=False,
                 regime_deleverage=False,
                 confidence_k=False,
                 mid_hold_review=False,
                 breadth_regime=False,
                 candidate_breadth=False,
                 theme_breadth=False,
                 dynamic_sector_cap=False,
                 gap_aware_sizing=False,
                 cluster_penalty=False,
                 macro_regime=False,
                 batch_entry=1,
                 dynamic_topk=False,
                 dynamic_gap_filter=False,
                 dynamic_corr_filter=False,
                 regime_sizing=False,
                 strong_regime_mult=1.25,
                 strong_breadth_min=0.55,
                 strong_vix_max=20.0,
                 max_regime_scale=1.50,
                 strong_tiers=None,
                 inst_hold_exit=False,
                 inst_hold_min_score=0.15,
                 inst_partial_min_score=-0.10,
                 inst_partial_frac=0.50,
                 inst_max_extend_days=10,
                 low_wr_rr_gate=False,
                 low_wr_threshold=0.45,
                 low_wr_lookback=20,
                 low_wr_min_trades=5,
                 low_wr_min_rr=2.0,
                 low_wr_rr_slope=2.0,
                 low_wr_min_tp=0.08,
                 low_wr_tp_slope=0.10,
                 sector_flow_tilt=False,
                 tilt_strength=1.0,
                 tilt_windows=None,
                 buy_cost=0.001425, sell_cost=0.004425,
                 # v9 Hybrid Tiered Risk Budgeting
                 hybrid_tiered=False,
                 core_tickers=None,
                 target_ann_vol=0.15,
                 rotation_trigger_vol=None,
                 crisis_vol=None,
                 cooling_days=None,
                 cooling_sat_boost=None,
                 cooling_core_boost=None,
                 stress_sat_floor=None,
                 stress_core_ceiling=None,
                 sat_alpha_trim_frac=None,
                 sat_alpha_trim_min_pnl=None,
                 core_alpha_trim_frac=None,
                 core_alpha_trim_min_pnl=None,
                 core_base_exposure=0.25,
                 tiered_core_decay=0.35,
                 tiered_sat_decay=0.85,
                 tiered_core_floor=0.55,
                 tiered_sat_floor=0.15):
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold_days = max_hold_days
        self.initial_capital = initial_capital
        self.position_size = position_size
        self.tp_sl_mode = tp_sl_mode
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.trailing_stop = trailing_stop
        self.trailing_atr_mult = trailing_atr_mult
        self.regime_filter = regime_filter
        self.regime_graduated = regime_graduated
        self.regime_floor = regime_floor
        self.gap_filter_atr = gap_filter_atr
        self.volume_confirm = volume_confirm
        self.blacklist_lookback = blacklist_lookback
        self.blacklist_min_wr = blacklist_min_wr
        self.breakeven_pct = breakeven_pct
        self.slippage = slippage
        self.vol_parity = vol_parity
        self.mean_reversion = mean_reversion
        self.dynamic_risk = dynamic_risk
        self.futures_hedge = futures_hedge
        self.dd_pause_pct = dd_pause_pct
        self.dd_pause_days = dd_pause_days
        self.consec_loss_limit = consec_loss_limit
        self.consec_loss_pause = consec_loss_pause
        self.sector_max_pct = sector_max_pct
        self.corr_filter = corr_filter
        # corr_select（建議A）：greedy 相關性選股。>0 啟用：依評分序選股，候選與
        # (現有持倉 ∪ 今日已選) 中相關係數 > corr_select_max 的檔數達 corr_select_cap 即跳過。
        # 用 corr_select_window 日(預設60)日報酬相關，資料只取到 i-1（無前視）。
        self.corr_select_max = corr_select_max
        self.corr_select_window = corr_select_window
        self.corr_select_cap = corr_select_cap
        self.max_portfolio_heat = max_portfolio_heat
        self.rank_weighted = rank_weighted
        self.regime_deleverage = regime_deleverage
        self.confidence_k = confidence_k
        self.mid_hold_review = mid_hold_review
        self.breadth_regime = breadth_regime
        self.candidate_breadth = candidate_breadth
        self.theme_breadth = theme_breadth
        self.dynamic_sector_cap = dynamic_sector_cap
        self.gap_aware_sizing = gap_aware_sizing
        self.cluster_penalty = cluster_penalty
        self.macro_regime = macro_regime
        self.batch_entry = batch_entry
        self.dynamic_topk = dynamic_topk
        self.dynamic_gap_filter = dynamic_gap_filter
        self.dynamic_corr_filter = dynamic_corr_filter
        self.regime_sizing = regime_sizing
        self.strong_regime_mult = strong_regime_mult
        self.strong_breadth_min = strong_breadth_min
        self.strong_vix_max = strong_vix_max
        self.max_regime_scale = max_regime_scale
        # 分段強勢加碼：list of (breadth_min, vix_max, mult)，條件越強倍數越大；
        # None 時退回單段 strong_regime_mult（向後相容）。
        self.strong_tiers = strong_tiers
        self.inst_hold_exit = inst_hold_exit
        self.inst_hold_min_score = inst_hold_min_score
        self.inst_partial_min_score = inst_partial_min_score
        self.inst_partial_frac = inst_partial_frac
        self.inst_max_extend_days = inst_max_extend_days
        self.low_wr_rr_gate = low_wr_rr_gate
        self.low_wr_threshold = low_wr_threshold
        self.low_wr_lookback = low_wr_lookback
        self.low_wr_min_trades = low_wr_min_trades
        self.low_wr_min_rr = low_wr_min_rr
        self.low_wr_rr_slope = low_wr_rr_slope
        self.low_wr_min_tp = low_wr_min_tp
        self.low_wr_tp_slope = low_wr_tp_slope
        self.sector_flow_tilt = sector_flow_tilt
        self.tilt_strength = tilt_strength
        self.tilt_windows = tilt_windows if tilt_windows else [10, 15, 20]
        self.buy_cost = buy_cost
        self.sell_cost = sell_cost
        # v9
        self.hybrid_tiered = hybrid_tiered
        self.core_tickers = set(core_tickers or ['2330', '2454', '2308', '2317'])
        self.target_ann_vol = target_ann_vol
        from strategy.portfolio_vol_target import (
            ROTATION_TRIGGER_VOL, CRISIS_VOL,
            COOLING_DAYS_DEFAULT, COOLING_SAT_BOOST_DEFAULT,
            COOLING_CORE_BOOST_DEFAULT, STRESS_SAT_FLOOR_DEFAULT,
            STRESS_CORE_CEILING_DEFAULT, SAT_ALPHA_TRIM_FRAC_DEFAULT,
            SAT_ALPHA_TRIM_MIN_PNL_DEFAULT, CORE_ALPHA_TRIM_FRAC_DEFAULT,
            CORE_ALPHA_TRIM_MIN_PNL_DEFAULT,
        )
        if rotation_trigger_vol is None:
            rotation_trigger_vol = ROTATION_TRIGGER_VOL
        if crisis_vol is None:
            crisis_vol = CRISIS_VOL
        self.rotation_trigger_vol = rotation_trigger_vol
        self.crisis_vol = crisis_vol
        self.cooling_days = cooling_days if cooling_days is not None else COOLING_DAYS_DEFAULT
        self.cooling_sat_boost = (
            cooling_sat_boost if cooling_sat_boost is not None else COOLING_SAT_BOOST_DEFAULT
        )
        self.cooling_core_boost = (
            cooling_core_boost if cooling_core_boost is not None else COOLING_CORE_BOOST_DEFAULT
        )
        self.stress_sat_floor = (
            stress_sat_floor if stress_sat_floor is not None else STRESS_SAT_FLOOR_DEFAULT
        )
        self.stress_core_ceiling = (
            stress_core_ceiling if stress_core_ceiling is not None else STRESS_CORE_CEILING_DEFAULT
        )
        self.sat_alpha_trim_frac = (
            sat_alpha_trim_frac if sat_alpha_trim_frac is not None else SAT_ALPHA_TRIM_FRAC_DEFAULT
        )
        self.sat_alpha_trim_min_pnl = (
            sat_alpha_trim_min_pnl if sat_alpha_trim_min_pnl is not None else SAT_ALPHA_TRIM_MIN_PNL_DEFAULT
        )
        self.core_alpha_trim_frac = (
            core_alpha_trim_frac if core_alpha_trim_frac is not None else CORE_ALPHA_TRIM_FRAC_DEFAULT
        )
        self.core_alpha_trim_min_pnl = (
            core_alpha_trim_min_pnl if core_alpha_trim_min_pnl is not None else CORE_ALPHA_TRIM_MIN_PNL_DEFAULT
        )
        self.core_base_exposure = core_base_exposure
        self.tiered_core_decay = tiered_core_decay
        self.tiered_sat_decay = tiered_sat_decay
        self.tiered_core_floor = tiered_core_floor
        self.tiered_sat_floor = tiered_sat_floor
        self._book_log = []  # for reporting which positions were core/sat
        self._tiered_scales_log = []
        self._last_fvol = None
        self._daily_rotation = {}
        self._daily_sat_trade_scale = 1.0
        self._last_dd_pause_pct = dd_pause_pct
        self._cooling_days_left = 0

    def _compute_atr(self, high_df, low_df, close_df, period=20):
        """計算精確的 ATR（True Range 的移動平均）。"""
        prev_close = close_df.shift(1)
        tr1 = high_df - low_df
        tr2 = (high_df - prev_close).abs()
        tr3 = (low_df - prev_close).abs()

        # element-wise max
        true_range = np.maximum(np.maximum(tr1, tr2), tr3)
        if isinstance(true_range, np.ndarray):
            true_range = pd.DataFrame(true_range, index=high_df.index, columns=high_df.columns)

        atr = true_range.rolling(period).mean()
        return atr

    def _inst_flow_value(self, inst_flow_by_window, window, ticker, idx):
        if not inst_flow_by_window or window not in inst_flow_by_window:
            return np.nan
        df = inst_flow_by_window[window]
        if df is None or ticker not in df.columns or idx < 0:
            return np.nan
        try:
            series = df[ticker].iloc[:idx + 1].ffill()
            if series.empty:
                return np.nan
            return float(series.iloc[-1])
        except Exception:
            return np.nan

    def _institutional_exit_action(self, ticker, idx, trade, inst_flow_by_window,
                                   profit_pct, exit_reason):
        if (not self.inst_hold_exit
                or not inst_flow_by_window
                or profit_pct <= 0
                or ('停利' not in exit_reason and '時限' not in exit_reason)):
            return 'full', {}

        if trade.get('days_held', 0) >= self.max_hold_days + self.inst_max_extend_days:
            return 'full', {'inst_score': np.nan, 'inst_note': 'extension_limit'}

        lookup_idx = idx - 1
        flows = {
            5: self._inst_flow_value(inst_flow_by_window, 5, ticker, lookup_idx),
            10: self._inst_flow_value(inst_flow_by_window, 10, ticker, lookup_idx),
            20: self._inst_flow_value(inst_flow_by_window, 20, ticker, lookup_idx),
        }
        valid = [v for v in flows.values() if not pd.isna(v)]
        if len(valid) < 2:
            return 'full', {'inst_score': np.nan, 'inst_note': 'insufficient_inst_data'}

        score = (
            (0.50 * flows[5] if not pd.isna(flows[5]) else 0.0)
            + (0.30 * flows[10] if not pd.isna(flows[10]) else 0.0)
            + (0.20 * flows[20] if not pd.isna(flows[20]) else 0.0)
        )
        positives = sum(1 for v in valid if v > 0)
        note = {
            'inst_5d': flows[5],
            'inst_10d': flows[10],
            'inst_20d': flows[20],
            'inst_score': score,
        }

        if positives >= 2 and score >= self.inst_hold_min_score:
            return 'hold', note
        if (score >= self.inst_partial_min_score
                and not trade.get('inst_partial_taken', False)):
            return 'partial', note
        return 'full', note

    def _low_wr_rr_reject(self, ticker, ticker_history, tp_price, sl_price, entry_price):
        if not self.low_wr_rr_gate or ticker not in ticker_history:
            return False, {}

        recent = ticker_history[ticker][-self.low_wr_lookback:]
        if len(recent) < self.low_wr_min_trades:
            return False, {}

        win_rate = sum(1 for r in recent if r > 0) / len(recent)
        if win_rate >= self.low_wr_threshold:
            return False, {'win_rate': win_rate}

        upside = max(0.0, (tp_price / entry_price) - 1)
        downside = max(1e-8, (entry_price - sl_price) / entry_price)
        rr = upside / downside
        wr_gap = self.low_wr_threshold - win_rate
        required_rr = self.low_wr_min_rr + wr_gap * self.low_wr_rr_slope
        required_tp = self.low_wr_min_tp + wr_gap * self.low_wr_tp_slope
        reject = rr < required_rr or upside < required_tp
        return reject, {
            'win_rate': win_rate,
            'rr': rr,
            'required_rr': required_rr,
            'tp_return': upside,
            'required_tp': required_tp,
        }

    def _corr_select(self, candidates, active_trades, close_df, i, k_slots):
        """建議A：greedy 相關性選股（corr_select_max/window/cap）。

        依評分序走訪候選；候選與「現有持倉 ∪ 今日已選」中 corr_select_window 日
        報酬相關 > corr_select_max 的檔數達 corr_select_cap 即跳過（選下一名）。
        被跳過者不回補——寧缺勿濫，讓部位真正分散（有效注數↑）。
        相關窗只取到 i-1（進場信號同一資訊集，無前視）。資料不足時退回原排序。
        """
        if k_slots <= 0:
            return []
        try:
            win = int(self.corr_select_window)
            held = [t for t in active_trades.keys() if t in close_df.columns]
            cand_tickers = [c[0] for c in candidates[:25] if c[0] in close_df.columns]
            pool = list(dict.fromkeys(held + cand_tickers))
            if len(pool) < 2:
                return candidates[:k_slots]
            ret_slice = close_df[pool].iloc[max(0, i - win):i].pct_change()
            ret_slice = ret_slice.dropna(how='all')
            if len(ret_slice) < 20:
                return candidates[:k_slots]
            corr = ret_slice.corr(min_periods=max(20, int(win * 0.5)))
            picked = []
            compare_set = list(held)          # 對照集 = 持倉 ∪ 今日已選
            for ticker, score, ep in candidates:
                if len(picked) >= k_slots:
                    break
                peers = [t for t in compare_set if t != ticker and t in corr.columns]
                if peers and ticker in corr.index:
                    high_n = int((corr.loc[ticker, peers] > self.corr_select_max).sum())
                    if high_n >= self.corr_select_cap:
                        continue                # 與組合高相關 → 跳過選下一名
                picked.append((ticker, score, ep))
                compare_set.append(ticker)
            return picked
        except Exception:
            return candidates[:k_slots]

    def run(self, total_score, close_df, open_df, high_df, low_df, ma_60,
            top_k=3, threshold=2.0, atr_df=None,
            market_close=None, vol_df=None, universe_mask=None,
            inst_flow_by_window=None, vix_series=None):
        """
        執行事件驅動回測。

        Parameters
        ----------
        total_score : pd.DataFrame
            AI 綜合評分矩陣 (日期 x 股票)
        close_df : pd.DataFrame
            收盤價矩陣
        open_df : pd.DataFrame
            開盤價矩陣（v2: 用於 t+1 open 進場）
        high_df : pd.DataFrame
            最高價矩陣
        low_df : pd.DataFrame
            最低價矩陣
        ma_60 : pd.DataFrame
            60 日均線矩陣（進場過濾條件）
        top_k : int
            每日最多進場股票數（預設 3）
        threshold : float
            安全下限門檻（score < threshold 不進場，預設 2.0）
        atr_df : pd.DataFrame, optional
            預計算的 ATR 矩陣（若未提供且 mode='atr'，則內部計算）
        market_close : pd.Series, optional
            大盤指數收盤價（0050），用於 regime filter
        vol_df : pd.DataFrame, optional
            成交量矩陣，用於 volume confirmation

        Returns
        -------
        trades_df : pd.DataFrame
            所有已完成交易的明細
        equity_df : pd.DataFrame
            每日資金曲線
        """
        mode_desc = f"ATR×{self.tp_atr_mult}/{self.sl_atr_mult}" if self.tp_sl_mode == 'atr' \
            else f"+{self.tp_pct*100:.0f}%/-{self.sl_pct*100:.0f}%"
        if self.trailing_stop:
            mode_desc += f" +Trailing({self.trailing_atr_mult}×ATR)"
        filters = []
        if self.regime_filter:
            filters.append('Regime')
        if self.gap_filter_atr > 0:
            filters.append(f'Gap({self.gap_filter_atr}×ATR)')
        if self.volume_confirm:
            filters.append('VolConfirm')
        if self.blacklist_lookback > 0:
            filters.append(f'Blacklist({self.blacklist_lookback})')
        if self.mean_reversion:
            filters.append('MeanRev')
        if self.dynamic_risk:
            filters.append('DynRisk')
        if self.futures_hedge:
            filters.append('FutHedge')
        filter_desc = f" Filters: {'+'.join(filters)}" if filters else ""
        cost_desc = f"買 {self.buy_cost*100:.3f}% 賣 {self.sell_cost*100:.3f}%"

        print(f"💰 執行精準區間回測 (TP/SL: {mode_desc}, "
              f"Top-{top_k}, 最長持有 {self.max_hold_days} 天, "
              f"成本: {cost_desc}{filter_desc})...")

        # 存 universe_mask 供 breadth regime 使用
        self._universe_mask = universe_mask
        # 預計算 20MA 供 breadth 重用（避免迴圈內反覆 rolling）
        self._ma20_all = close_df.rolling(20).mean() if self.breadth_regime else None

        # 宏觀 Regime / v9：下載 VIX（VIX>33 建倉規則）
        self._vix_series = vix_series
        if self._vix_series is None and (self.macro_regime or self.hybrid_tiered or self.regime_sizing):
            try:
                import yfinance as yf
                vix = yf.download('^VIX', start=close_df.index[0], end=close_df.index[-1], progress=False)
                if 'Close' in vix.columns:
                    self._vix_series = vix['Close'].squeeze()
                elif ('Close', '^VIX') in vix.columns:
                    self._vix_series = vix[('Close', '^VIX')].squeeze()
                if self._vix_series is not None:
                    print(f"   🌍 Macro Regime: VIX 已載入 ({len(self._vix_series)} 天)")
            except Exception as e:
                print(f"   ⚠️ VIX 下載失敗: {e}")
        # === Sector Flow Tilt：預計算板塊動量 ===
        self._sector_flow_df = None
        self._sector_composition = None
        if self.sector_flow_tilt:
            try:
                from strategy.sector_flow import compute_sector_flow
                self._sector_flow_df, self._sector_composition = compute_sector_flow(
                    close_df, universe_mask, windows=self.tilt_windows
                )
                n_sectors = len(self._sector_composition)
                print(f"   📊 Sector Flow Tilt: {n_sectors} 板塊, "
                      f"窗口={self.tilt_windows}, 力度={self.tilt_strength}")
            except Exception as e:
                print(f"   ⚠️ Sector Flow Tilt 計算失敗: {e}")
                self._sector_flow_df = None

        # 計算精確 ATR（如果使用 ATR 模式）
        if self.tp_sl_mode == 'atr':
            if atr_df is None:
                atr = self._compute_atr(high_df, low_df, close_df)
            else:
                atr = atr_df
        else:
            atr = None

        # 大盤 60MA（regime filter）
        if self.regime_filter and market_close is not None:
            market_ma60 = market_close.rolling(60).mean()
            market_ma20 = market_close.rolling(20).mean()
        else:
            market_ma60 = None
            market_ma20 = None

        # 成交量 20 日均量
        if self.volume_confirm and vol_df is not None:
            vol_ma20 = vol_df.rolling(20).mean()
        else:
            vol_ma20 = None

        trades = []
        capital = self.initial_capital
        equity_curve = []
        dates = close_df.index
        active_trades = {}  # ticker -> trade_info
        max_positions = int(1.0 / self.position_size)  # 最多同時持有
        ticker_history = {}  # ticker -> list of recent Return_Pct (for blacklist)
        self._rejected_entry_log = []

        def is_tradable_bar(ticker, idx):
            """True only when the raw OHLCV bar can support a real fill."""
            required = (open_df, high_df, low_df, close_df)
            if any(ticker not in df.columns for df in required):
                return False
            vals = [
                open_df[ticker].iloc[idx],
                high_df[ticker].iloc[idx],
                low_df[ticker].iloc[idx],
                close_df[ticker].iloc[idx],
            ]
            if any(pd.isna(v) or v <= 0 for v in vals):
                return False
            if vol_df is not None and ticker in vol_df.columns:
                volume = vol_df[ticker].iloc[idx]
                if pd.isna(volume) or volume <= 0:
                    return False
            return True

        # === 動態風險預算：預計算市場 realized vol ===
        market_daily_ret = None
        if self.dynamic_risk and market_close is not None:
            market_daily_ret = market_close.pct_change()

        # === 台指期對沖追蹤 ===
        hedge_active = False
        hedge_entry_price = 0.0
        hedge_pnl_total = 0.0

        # === 均值回歸：計算超跌指標（RSI-like） ===
        if self.mean_reversion:
            delta = close_df.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / (loss + 1e-8)
            rsi_14 = 100 - (100 / (1 + rs))
            # 5 日跌幅
            ret_5d = close_df / close_df.shift(5) - 1

        # === 回撤竟日卡 + 連續停損追蹤 ===
        peak_equity = self.initial_capital
        dd_pause_counter = 0     # 回撤卡剩餘暫停天數
        consec_sl_count = 0      # 連續停損筆數
        cl_pause_counter = 0     # 連損卡剩餘暫停天數
        regime_below_count = 0   # 大盤連續低於 60MA 的天數

        # 從第 60 天開始（確保技術指標已穩定）
        for i in range(60, len(dates)):
            date = dates[i]

            # ── Step 1: 處理持倉的出場判定（根據今日盤中高低價） ──
            exited_tickers = []
            for ticker, trade in active_trades.items():
                trade['days_held'] += 1
                if not is_tradable_bar(ticker, i):
                    continue

                current_high = high_df[ticker].iloc[i]
                current_low = low_df[ticker].iloc[i]
                current_close = close_df[ticker].iloc[i]

                if pd.isna(current_close):
                    continue

                # 更新移動停利追蹤價（每日盤中最高價）
                if not pd.isna(current_high):
                    trade['highest_since_entry'] = max(
                        trade['highest_since_entry'], current_high
                    )

                    # 動態調整 trailing stop level
                    if self.trailing_stop and trade.get('atr_at_entry', 0) > 0:
                        trailing_sl = (trade['highest_since_entry']
                                       - trade['atr_at_entry'] * self.trailing_atr_mult)
                        # trailing SL 只能往上調，不能往下
                        trade['sl_price'] = max(trade['sl_price'], trailing_sl)

                    # ── Breakeven Stop：獲利超過 breakeven_pct 後將 SL 移至成本價 ──
                    if (self.breakeven_pct > 0
                            and not trade.get('breakeven_activated', False)):
                        unrealized = (current_high / trade['entry_price']) - 1
                        if unrealized >= self.breakeven_pct:
                            be_price = trade['entry_price']  # 成本價
                            trade['sl_price'] = max(trade['sl_price'], be_price)
                            trade['breakeven_activated'] = True

                exit_triggered = False
                exit_price = 0
                exit_reason = ""
                current_open = open_df[ticker].iloc[i] if ticker in open_df.columns else np.nan

                # 優先檢查停損 / trailing stop（保守回測法）
                # Gap-aware fill: 若開盤已穿越停損，成交價 = min(stop, open)
                if current_low <= trade['sl_price']:
                    exit_triggered = True
                    if not pd.isna(current_open) and current_open < trade['sl_price']:
                        exit_price = current_open  # gap down: 成交在開盤價
                    else:
                        exit_price = trade['sl_price']
                    # 區分初始停損 vs trailing stop 觸發
                    if (self.trailing_stop
                            and trade['sl_price'] > trade['initial_sl_price']):
                        exit_reason = "🟡 移動停利"
                    else:
                        exit_reason = "🔴 停損"
                elif (not self.trailing_stop) and current_high >= trade['tp_price']:
                    # Gap-aware fill: 若開盤已穿越停利，成交價 = max(tp, open)
                    exit_triggered = True
                    if not pd.isna(current_open) and current_open > trade['tp_price']:
                        exit_price = current_open  # gap up: 成交在開盤價（更有利）
                    else:
                        exit_price = trade['tp_price']
                    exit_reason = "🟢 停利"
                elif (self.mid_hold_review
                      and 10 <= trade['days_held'] <= 14
                      and current_close < trade['entry_price']):
                    # Mid-hold review: 持有 10-14 天仍虧損，且動量已衰退→提早出場
                    try:
                        ticker_score = total_score[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        if not pd.isna(ticker_score) and ticker_score < threshold:
                            exit_triggered = True
                            exit_price = current_close
                            exit_reason = "🟠 汰弱"
                    except Exception:
                        pass
                elif trade['days_held'] >= self.max_hold_days:
                    exit_triggered = True
                    exit_price = current_close
                    exit_reason = "⚪ 時間到期"

                if exit_triggered:
                    # 扣除賣出成本 + 滑價
                    exit_price_with_slippage = exit_price * (1 - self.slippage)
                    gross_revenue = trade['shares'] * exit_price_with_slippage * (1 - self.sell_cost)
                    gross_cost_in = trade['actual_cost']
                    gross_profit_pct = (gross_revenue - gross_cost_in) / gross_cost_in

                    inst_action, inst_note = self._institutional_exit_action(
                        ticker, i, trade, inst_flow_by_window,
                        gross_profit_pct, exit_reason,
                    )
                    if inst_action == 'hold':
                        trade['inst_exit_deferrals'] = trade.get('inst_exit_deferrals', 0) + 1
                        trade['last_inst_score'] = inst_note.get('inst_score')
                        continue

                    partial_frac = 0.0
                    if inst_action == 'partial':
                        partial_frac = min(max(self.inst_partial_frac, 0.05), 0.95)

                    exit_shares = trade['shares'] * (partial_frac if partial_frac else 1.0)
                    exit_cost_in = trade['actual_cost'] * (partial_frac if partial_frac else 1.0)
                    revenue = exit_shares * exit_price_with_slippage * (1 - self.sell_cost)
                    capital += revenue

                    # 計算含成本的真實報酬
                    total_cost_in = exit_cost_in
                    profit_pct = (revenue - total_cost_in) / total_cost_in
                    record_reason = exit_reason
                    if partial_frac:
                        record_reason = f"{exit_reason} / 法人分批停利"
                    elif inst_note:
                        record_reason = f"{exit_reason} / 法人確認"

                    trade_record = {
                        'Ticker': ticker,
                        'Entry_Date': trade['entry_date'].strftime('%Y-%m-%d'),
                        'Exit_Date': date.strftime('%Y-%m-%d'),
                        'Entry_Price': round(trade['entry_price'], 2),
                        'Exit_Price': round(exit_price, 2),
                        'Return_Pct': round(profit_pct, 4),
                        'Reason': record_reason,
                        'Days_Held': trade['days_held'],
                        'TP_Price': round(trade['tp_price'], 2),
                        'SL_Price': round(trade['sl_price'], 2),
                        'Partial_Fraction': round(partial_frac, 4) if partial_frac else 1.0,
                        'Inst_5D': inst_note.get('inst_5d'),
                        'Inst_10D': inst_note.get('inst_10d'),
                        'Inst_20D': inst_note.get('inst_20d'),
                        'Inst_Score': inst_note.get('inst_score'),
                        'Book': trade.get('book', 'satellite'),  # v9
                        'Tiered_Scale': trade.get('tiered_scale', 1.0),  # v9
                    }
                    trades.append(trade_record)

                    # 更新 per-stock 歷史（用於 blacklist）
                    if ticker not in ticker_history:
                        ticker_history[ticker] = []
                    ticker_history[ticker].append(profit_pct)

                    if partial_frac:
                        trade['shares'] -= exit_shares
                        trade['actual_cost'] -= exit_cost_in
                        trade['inst_partial_taken'] = True
                        trade['inst_exit_deferrals'] = trade.get('inst_exit_deferrals', 0) + 1
                        trade['last_inst_score'] = inst_note.get('inst_score')
                        if trade['shares'] > 1e-8 and trade['actual_cost'] > 0:
                            continue

                    exited_tickers.append(ticker)

                    # === 連續停損追蹤 ===
                    if '停損' in exit_reason:
                        consec_sl_count += 1
                        if consec_sl_count >= self.consec_loss_limit:
                            cl_pause_counter = self.consec_loss_pause
                            consec_sl_count = 0
                    else:
                        consec_sl_count = 0

            # 移除已出場的股票
            for t in exited_tickers:
                del active_trades[t]

            # ── Step 2: 計算當前總權益（用於 equity-based sizing） ──
            current_equity = capital
            for ticker, trade in active_trades.items():
                close_val = close_df[ticker].iloc[i]
                if not pd.isna(close_val):
                    current_equity += trade['shares'] * close_val

            # === 回撤竟日卡：權益距 peak 超過 N% 則暫停新倉 ===
            peak_equity = max(peak_equity, current_equity)
            current_dd = (current_equity - peak_equity) / peak_equity
            effective_dd_pause = (
                self._last_dd_pause_pct if self.hybrid_tiered else self.dd_pause_pct
            )
            if current_dd < -effective_dd_pause and dd_pause_counter <= 0:
                dd_pause_counter = self.dd_pause_days

            # 暫停計數器遞減
            if dd_pause_counter > 0:
                dd_pause_counter -= 1
            if cl_pause_counter > 0:
                cl_pause_counter -= 1

            # ── Step 2.5: Regime Deleverage：大盤翻空後分段降曝險 ──
            # ━━ FIX: 使用 t-1 大盤數據（避免同日 lookahead）━━
            if self.regime_deleverage and market_ma60 is not None and active_trades:
                try:
                    prev_date = dates[i - 1]
                    mkt_date = market_close.index.get_indexer([prev_date], method='ffill')[0]
                    if mkt_date >= 0:
                        mkt_val = market_close.iloc[mkt_date]
                        mkt_ma = market_ma60.iloc[mkt_date]
                        if not pd.isna(mkt_val) and not pd.isna(mkt_ma):
                            if mkt_val < mkt_ma:
                                regime_below_count += 1
                            else:
                                regime_below_count = 0

                            # Stage 1: 連續 2 天 < 60MA → 平掉虧損超過 -3% 的部位
                            if regime_below_count >= 2:
                                delev_tickers = []
                                for ticker, trade in active_trades.items():
                                    cur_price = close_df[ticker].iloc[i]
                                    if pd.isna(cur_price):
                                        continue
                                    unrealized = (cur_price / trade['entry_price']) - 1
                                    if unrealized < -0.03:
                                        # 強制出場：用當日收盤價
                                        exit_price_dv = cur_price * (1 - self.slippage)
                                        revenue = trade['shares'] * exit_price_dv * (1 - self.sell_cost)
                                        capital += revenue
                                        profit_pct = (exit_price_dv * (1 - self.sell_cost)) / \
                                                     (trade['entry_price'] * (1 + self.buy_cost)) - 1
                                        trades.append({
                                            'Ticker': ticker,
                                            'Entry_Date': trade['entry_date'],
                                            'Exit_Date': date,
                                            'Entry_Price': trade['entry_price'],
                                            'Exit_Price': cur_price,
                                            'Return_Pct': profit_pct,
                                            'Days_Held': trade['days_held'],
                                            'Reason': '🟠 Regime降曝',
                                            'TP_Price': trade['tp_price'],
                                            'SL_Price': trade['sl_price'],
                                        })
                                        delev_tickers.append(ticker)
                                        if ticker not in ticker_history:
                                            ticker_history[ticker] = []
                                        ticker_history[ticker].append(profit_pct)
                                for t in delev_tickers:
                                    del active_trades[t]
                except Exception:
                    pass

            # ── Step 2.6: v9 資金輪動（高波動→Core / 回落→Core獲利了結回流Sat）──
            self._daily_rotation = {
                'core_rotation_boost': 1.0,
                'sat_rotation_boost': 1.0,
                'rotate_sat_profits_to_core': 0.0,
                'rotate_core_profits_to_sat': 0.0,
                'sat_entry_freeze': 0.0,
                'dd_pause_pct': self.dd_pause_pct,
                'vol_regime': 'normal',
            }
            self._daily_sat_trade_scale = 1.0
            if self.hybrid_tiered and len(equity_curve) >= 60:
                try:
                    from strategy.portfolio_vol_target import PortfolioVolatilityTarget, VolTargetConfig
                    recent_eq = pd.Series([e['Equity'] for e in equity_curve[-60:]])
                    pvt = PortfolioVolatilityTarget(VolTargetConfig(
                        target_ann_vol=self.target_ann_vol,
                        rotation_trigger_vol=self.rotation_trigger_vol,
                        crisis_vol=self.crisis_vol,
                        cooling_days=self.cooling_days,
                        cooling_sat_boost=self.cooling_sat_boost,
                        cooling_core_boost=self.cooling_core_boost,
                        stress_sat_floor=self.stress_sat_floor,
                        stress_core_ceiling=self.stress_core_ceiling,
                        sat_alpha_trim_frac=self.sat_alpha_trim_frac,
                        sat_alpha_trim_min_pnl=self.sat_alpha_trim_min_pnl,
                        core_alpha_trim_frac=self.core_alpha_trim_frac,
                        core_alpha_trim_min_pnl=self.core_alpha_trim_min_pnl,
                        core_decay=self.tiered_core_decay,
                        sat_decay=self.tiered_sat_decay,
                        core_floor=self.tiered_core_floor,
                        sat_floor=self.tiered_sat_floor,
                        core_base_gross=self.core_base_exposure,
                    ))
                    pvt._prev_forecast_vol = self._last_fvol
                    pvt._cooling_days_left = self._cooling_days_left
                    fvol = pvt.forecast_portfolio_ann_vol(None, None, recent_eq)
                    scales = pvt.tiered_scale_factors(fvol)
                    if scales.get('cooling_transition', 0) >= 1.0:
                        self._cooling_days_left = self.cooling_days
                    elif self._cooling_days_left > 0:
                        self._cooling_days_left -= 1

                    self._daily_rotation = {
                        k: scales[k] for k in (
                            'core_rotation_boost', 'sat_rotation_boost',
                            'rotate_sat_profits_to_core', 'rotate_core_profits_to_sat',
                            'sat_entry_freeze', 'dd_pause_pct', 'vol_regime',
                            'stress_transition', 'cooling_active',
                            'sat_alpha_trim_frac', 'sat_alpha_trim_min_pnl',
                            'core_alpha_trim_frac', 'core_alpha_trim_min_pnl',
                        ) if k in scales
                    }
                    self._daily_sat_trade_scale = 1.0
                    self._last_fvol = fvol
                    self._last_dd_pause_pct = scales.get('dd_pause_pct', self.dd_pause_pct)

                    def _trim_book_alpha(book, trim_frac, min_pnl, reason):
                        nonlocal capital
                        for ticker, trade in list(active_trades.items()):
                            if trade.get('book') != book:
                                continue
                            cur = close_df[ticker].iloc[i] if ticker in close_df.columns else np.nan
                            if pd.isna(cur) or cur <= 0:
                                continue
                            unrealized = (cur / trade['entry_price']) - 1
                            if unrealized < min_pnl:
                                continue
                            shares_sell = int(trade['shares'] * trim_frac)
                            if shares_sell <= 0:
                                continue
                            exit_px = cur * (1 - self.slippage)
                            revenue = shares_sell * exit_px * (1 - self.sell_cost)
                            capital += revenue
                            cost_basis = trade['actual_cost'] * (shares_sell / trade['shares'])
                            profit_pct = (revenue - cost_basis) / cost_basis if cost_basis > 0 else 0
                            trade['shares'] -= shares_sell
                            trade['actual_cost'] -= cost_basis
                            trades.append({
                                'Ticker': ticker,
                                'Entry_Date': trade['entry_date'].strftime('%Y-%m-%d'),
                                'Exit_Date': date.strftime('%Y-%m-%d'),
                                'Entry_Price': round(trade['entry_price'], 2),
                                'Exit_Price': round(cur, 2),
                                'Return_Pct': round(profit_pct, 4),
                                'Reason': reason,
                                'Days_Held': trade['days_held'],
                                'TP_Price': round(trade['tp_price'], 2),
                                'SL_Price': round(trade['sl_price'], 2),
                                'Book': book,
                                'Tiered_Scale': 1.0,
                            })

                    # regime 條件式放行：確認強多頭（0050 > MA60 且 > MA20，且 vol regime
                    # 正常）時，抑制「核心獲利了結回流 Sat」與冷卻續砍，讓核心贏家在多頭續抱。
                    # 危機側（波動升破→Sat 回流 Core / freeze / stress）完全不動。
                    if self._daily_rotation.get('rotate_sat_profits_to_core', 0) >= 1.0:
                        _trim_book_alpha(
                            'satellite',
                            self.sat_alpha_trim_frac,
                            self.sat_alpha_trim_min_pnl,
                            '🟠 輪動至Core(波動升破)',
                        )
                    if self._daily_rotation.get('rotate_core_profits_to_sat', 0) >= 1.0:
                        _trim_book_alpha(
                            'core',
                            self.core_alpha_trim_frac,
                            self.core_alpha_trim_min_pnl,
                            '🔵 輪動回Sat(波動回落)',
                        )
                    elif self._cooling_days_left > 0:
                        _trim_book_alpha(
                            'core',
                            0.20,
                            0.04,
                            '🔵 輪動回Sat(冷卻續跑)',
                        )
                except Exception:
                    pass

            # ── Step 3: 處理今日進場（根據昨日收盤信號，今日 open 進場） ──
            entry_allowed = (dd_pause_counter <= 0 and cl_pause_counter <= 0)

            if len(active_trades) < max_positions and entry_allowed:
                # ── Regime Filter + Graduated Exposure ──
                # ━━ FIX: 使用 t-1 大盤數據（避免同日 lookahead） ━━
                regime_ok = True
                regime_scale = 1.0  # 曝險縮放（graduated mode）
                market_strong = False
                breadth_pct = np.nan
                vix_val_for_regime = np.nan
                if market_ma60 is not None:
                    try:
                        prev_date = dates[i - 1]
                        mkt_date = market_close.index.get_indexer([prev_date], method='ffill')[0]
                        if mkt_date >= 0:
                            mkt_val = market_close.iloc[mkt_date]
                            mkt_ma60 = market_ma60.iloc[mkt_date]
                            mkt_ma20 = market_ma20.iloc[mkt_date] if market_ma20 is not None else np.nan
                            if not pd.isna(mkt_val) and not pd.isna(mkt_ma60):
                                if self.regime_graduated:
                                    # 四段式曝險：100% / 70% / 40% / 0%
                                    above_60 = mkt_val > mkt_ma60
                                    above_20 = mkt_val > mkt_ma20 if not pd.isna(mkt_ma20) else above_60
                                    market_strong = bool(above_60 and above_20)
                                    if above_60 and above_20:
                                        regime_scale = 1.0   # 強多頭：全力進場
                                    elif above_60 and not above_20:
                                        regime_scale = 0.7   # 轉弱警告：縮減 30%
                                    elif not above_60 and above_20:
                                        regime_scale = 0.4   # 初步轉強：保守進場
                                    else:
                                        if self.regime_floor > 0:
                                            regime_scale = self.regime_floor
                                        else:
                                            regime_scale = 0.0
                                            regime_ok = False
                                else:
                                    # 傳統 binary：大盤 > 60MA 才進場
                                    regime_ok = mkt_val > mkt_ma60
                    except Exception:
                        pass

                # === Breadth-aware Regime：用 universe 內部狀態修正 regime ===
                if self.breadth_regime and regime_ok and i >= 21 and self._ma20_all is not None:
                    try:
                        above_20ma = (close_df.iloc[i - 1] > self._ma20_all.iloc[i - 1])
                        if self._universe_mask is not None and i - 1 < len(self._universe_mask):
                            day_univ = self._universe_mask.iloc[i - 1]
                            above_20ma = above_20ma & day_univ
                            total_in_univ = max(day_univ.sum(), 1)
                        else:
                            total_in_univ = len(close_df.columns)
                        breadth_pct = above_20ma.sum() / total_in_univ

                        if breadth_pct < 0.30:
                            regime_scale = min(regime_scale, 0.3)
                        elif breadth_pct < 0.45:
                            regime_scale = min(regime_scale, 0.5)
                    except Exception:
                        pass

                # === Macro Regime：VIX（v9 不在此壓全局 regime；改於單筆 sizing 處理）===
                # legacy v8.5 + macro_regime：VIX>33 允許建倉，僅 22~33 溫和降曝險
                if self.macro_regime and not self.hybrid_tiered and self._vix_series is not None and regime_ok:
                    try:
                        prev_date = dates[i - 1]
                        vix_idx = self._vix_series.index.get_indexer([prev_date], method='ffill')[0]
                        if vix_idx >= 0:
                            vix_val = float(self._vix_series.iloc[vix_idx])
                            vix_val_for_regime = vix_val
                            if vix_val > 33:
                                pass  # 高恐慌：依策略訊號建倉，不額外壓制
                            elif vix_val > 30:
                                regime_scale *= 0.5
                            elif vix_val > 25:
                                regime_scale *= 0.7
                            elif vix_val > 22:
                                regime_scale *= 0.85
                    except Exception:
                        pass

                if (self.regime_sizing and regime_ok and market_strong
                        and not pd.isna(breadth_pct)
                        and breadth_pct >= self.strong_breadth_min):
                    if self._vix_series is not None and pd.isna(vix_val_for_regime):
                        try:
                            prev_date = dates[i - 1]
                            vix_idx = self._vix_series.index.get_indexer([prev_date], method='ffill')[0]
                            if vix_idx >= 0:
                                vix_val_for_regime = float(self._vix_series.iloc[vix_idx])
                        except Exception:
                            pass
                    if not pd.isna(vix_val_for_regime) and vix_val_for_regime <= self.strong_vix_max:
                        # 分段強勢加碼：基段 = strong_regime_mult；若 strong_tiers 提供，
                        # 取「breadth 更高且 VIX 更低」的更高段倍數（條件越強加碼越大）。
                        boost_mult = self.strong_regime_mult
                        if self.strong_tiers:
                            for t_breadth, t_vix, t_mult in self.strong_tiers:
                                if (breadth_pct >= t_breadth
                                        and vix_val_for_regime <= t_vix):
                                    boost_mult = max(boost_mult, t_mult)
                        regime_scale = min(
                            self.max_regime_scale,
                            regime_scale * boost_mult,
                        )

                candidates = []
                if regime_ok:
                    # ── 動量策略（正常模式） ──
                    for ticker in close_df.columns:
                        if ticker in active_trades:
                            continue

                        if self.blacklist_lookback > 0 and ticker in ticker_history:
                            recent = ticker_history[ticker][-self.blacklist_lookback:]
                            if len(recent) >= self.blacklist_lookback:
                                wr = sum(1 for r in recent if r > 0) / len(recent)
                                if wr < self.blacklist_min_wr:
                                    continue

                        score = total_score[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        ma = ma_60[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        prev_close = close_df[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        entry_price = open_df[ticker].iloc[i]

                        if not is_tradable_bar(ticker, i):
                            continue
                        if pd.isna(entry_price) or pd.isna(score) or pd.isna(ma):
                            continue
                        if pd.isna(prev_close) or entry_price <= 0:
                            continue

                        if not (score >= threshold and prev_close > ma):
                            continue

                        if self.gap_filter_atr > 0 and atr is not None:
                            atr_val = atr[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                            if not pd.isna(atr_val) and atr_val > 0:
                                gap = abs(entry_price - prev_close)
                                # Dynamic gap filter: 強勢 regime 放寬到 2.0 ATR
                                eff_gap_limit = self.gap_filter_atr
                                if self.dynamic_gap_filter:
                                    if regime_scale >= 1.0:
                                        eff_gap_limit = 2.0
                                    elif regime_scale >= 0.7:
                                        eff_gap_limit = 1.8
                                if gap > eff_gap_limit * atr_val:
                                    continue

                        # ━━ FIX: 使用 t-1 成交量（避免同日 lookahead——開盤時不知道今天總量） ━━
                        if vol_ma20 is not None and ticker in vol_df.columns:
                            prev_vol = vol_df[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                            avg_vol = vol_ma20[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                            if not pd.isna(prev_vol) and not pd.isna(avg_vol) and avg_vol > 0:
                                if prev_vol < avg_vol:
                                    continue

                        candidates.append((ticker, score, entry_price))

                elif self.mean_reversion and not regime_ok:
                    # ── 均值回歸子策略（熊市模式） ──
                    # 大盤 < 60MA 時，找超跌反彈股：RSI<30 且 5 日跌幅 > 10%
                    for ticker in close_df.columns:
                        if ticker in active_trades:
                            continue
                        entry_price = open_df[ticker].iloc[i]
                        prev_close = close_df[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        if not is_tradable_bar(ticker, i):
                            continue
                        if pd.isna(entry_price) or pd.isna(prev_close) or entry_price <= 0:
                            continue

                        ticker_rsi = rsi_14[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        ticker_ret5 = ret_5d[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        ma = ma_60[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan

                        if pd.isna(ticker_rsi) or pd.isna(ticker_ret5) or pd.isna(ma):
                            continue

                        # 反轉條件：RSI < 30 且 5 日跌超過 10%
                        if ticker_rsi < 30 and ticker_ret5 < -0.10:
                            # 反轉分數：RSI 越低越好
                            rev_score = (30 - ticker_rsi) + abs(ticker_ret5) * 100
                            candidates.append((ticker, rev_score, entry_price))

                # === 台指期對沖：大盤 < 60MA 時開空單 ===
                if self.futures_hedge and market_ma60 is not None:
                    try:
                        prev_date = dates[i - 1]
                        mkt_date = market_close.index.get_indexer([prev_date], method='ffill')[0]
                        if mkt_date >= 0:
                            mkt_val = market_close.iloc[mkt_date]
                            mkt_ma = market_ma60.iloc[mkt_date]
                            if not pd.isna(mkt_val) and not pd.isna(mkt_ma):
                                if mkt_val < mkt_ma and not hedge_active:
                                    # 開空單（模擬：用 10% 權益做空大盤）
                                    hedge_active = True
                                    hedge_entry_price = mkt_val
                                elif mkt_val >= mkt_ma and hedge_active:
                                    # 平空單
                                    hedge_return = (hedge_entry_price / mkt_val) - 1
                                    hedge_pnl = current_equity * 0.10 * hedge_return
                                    capital += hedge_pnl
                                    hedge_pnl_total += hedge_pnl
                                    hedge_active = False
                    except Exception:
                        pass

                # Top-K 選股：按分數排序，取前 top_k 名（含板塊分散）
                candidates.sort(key=lambda x: x[1], reverse=True)
                slots_available = max_positions - len(active_trades)

                # 板塊分散：電子股不超過 sector_max_pct
                # Dynamic sector cap: regime 越弱限制越緊
                if self.dynamic_sector_cap:
                    if regime_scale <= 0.4:
                        current_sector_cap = 0.25
                    elif regime_scale <= 0.7:
                        current_sector_cap = 0.4
                    else:
                        current_sector_cap = self.sector_max_pct
                else:
                    current_sector_cap = self.sector_max_pct

                if current_sector_cap < 1.0:
                    elec_prefixes = ('23','24','30','33','34','35','36','37',
                                     '49','61','63','64','65','66','67','68','69')
                    active_elec = sum(1 for t in active_trades if t.startswith(elec_prefixes))
                    max_elec_total = max(1, int(max_positions * current_sector_cap))

                    filtered_candidates = []
                    new_elec = 0
                    for c in candidates:
                        is_elec = c[0].startswith(elec_prefixes)
                        if is_elec and (active_elec + new_elec) >= max_elec_total:
                            continue  # 電子股已滿，跳過
                        filtered_candidates.append(c)
                        if is_elec:
                            new_elec += 1
                    candidates = filtered_candidates

                # Confidence-K: 動態調整 top_k
                # === Candidate Breadth：前 15 名動量品質檢查 ===
                if self.candidate_breadth and len(candidates) >= 5 and self._ma20_all is not None:
                    try:
                        top15 = [c[0] for c in candidates[:15]]
                        above = 0
                        total = 0
                        for t in top15:
                            if t in close_df.columns:
                                p = close_df[t].iloc[i - 1] if i - 1 >= 0 else np.nan
                                m = self._ma20_all[t].iloc[i - 1] if i - 1 >= 0 else np.nan
                                if not pd.isna(p) and not pd.isna(m):
                                    total += 1
                                    if p > m:
                                        above += 1
                        if total >= 5:
                            cand_breadth = above / total
                            if cand_breadth < 0.40:
                                regime_scale = min(regime_scale, 0.4)
                            elif cand_breadth < 0.55:
                                regime_scale = min(regime_scale, 0.6)
                    except Exception:
                        pass

                # === Theme Breadth：前 15 名板塊集中度檢查 ===
                if self.theme_breadth and len(candidates) >= 5:
                    try:
                        top15 = [c[0] for c in candidates[:15]]
                        elec_prefixes = ('23','24','30','33','34','35','36','37',
                                         '49','61','63','64','65','66','67','68','69')
                        elec_count = sum(1 for t in top15 if str(t).startswith(elec_prefixes))
                        theme_ratio = elec_count / len(top15)
                        if theme_ratio > 0.80:
                            regime_scale = min(regime_scale, 0.6)
                        elif theme_ratio > 0.70:
                            regime_scale = min(regime_scale, 0.75)
                    except Exception:
                        pass

                effective_top_k = top_k
                # Dynamic Top-K: 弱勢 regime 自動降低持股數
                if self.dynamic_topk:
                    if regime_scale <= 0.3:
                        effective_top_k = max(2, top_k - 4)
                    elif regime_scale <= 0.5:
                        effective_top_k = max(3, top_k - 3)
                    elif regime_scale <= 0.7:
                        effective_top_k = max(4, top_k - 2)

                if self.confidence_k and len(candidates) >= 3:
                    scores = [c[1] for c in candidates[:min(top_k + 3, len(candidates))]]
                    top_score = scores[0]
                    if top_score > 0:
                        # 只選分數 >= top_score * 0.6 的候選
                        quality_count = sum(1 for s in scores if s >= top_score * 0.6)
                        effective_top_k = max(2, min(effective_top_k, quality_count))

                # === Cluster-Penalized Selection ===
                # 對候選股分數做 correlation-based soft penalty
                if self.cluster_penalty and len(candidates) >= 3 and i >= 22:
                    try:
                        lookback = min(20, i)
                        cand_tickers = [c[0] for c in candidates[:min(15, len(candidates))]]
                        held_tickers = list(active_trades.keys())
                        all_tickers = list(set(cand_tickers + held_tickers))
                        valid_tickers = [t for t in all_tickers if t in close_df.columns]

                        if len(valid_tickers) >= 3 and lookback >= 10:
                            ret_slice = close_df[valid_tickers].iloc[max(0, i-lookback):i].pct_change().dropna()
                            if len(ret_slice) >= 5:
                                corr_mat = ret_slice.corr()
                                penalized = []
                                already_in = set(held_tickers)
                                for ticker, score, ep in candidates:
                                    if ticker in corr_mat.index and len(already_in) > 0:
                                        valid_peers = [t for t in already_in
                                                       if t in corr_mat.columns and t != ticker]
                                        if valid_peers:
                                            avg_corr = corr_mat.loc[ticker, valid_peers].mean()
                                            if avg_corr > 0.7:
                                                score = score * 0.7
                                            elif avg_corr > 0.5:
                                                score = score * 0.85
                                    penalized.append((ticker, score, ep))
                                    already_in.add(ticker)
                                candidates = sorted(penalized, key=lambda x: x[1], reverse=True)
                    except Exception:
                        pass

                # === Sector Flow Tilt：按板塊資金流分配 slot ===
                if (self.sector_flow_tilt and self._sector_flow_df is not None
                        and i - 1 >= 0):
                    try:
                        from strategy.sector_flow import get_sector_slots, select_with_sector_tilt
                        day_sector_scores = self._sector_flow_df.iloc[i - 1]
                        sector_slots = get_sector_slots(
                            day_sector_scores,
                            top_k=effective_top_k,
                            tilt_strength=self.tilt_strength,
                        )
                        if sector_slots:
                            selected = select_with_sector_tilt(
                                candidates, sector_slots,
                                effective_top_k, slots_available,
                            )
                        else:
                            selected = candidates[:min(effective_top_k, slots_available)]
                    except Exception:
                        selected = candidates[:min(effective_top_k, slots_available)]
                elif self.corr_select_max > 0:
                    # 建議A：greedy 相關性選股（與持倉∪已選 60日相關>閾值即跳過）
                    selected = self._corr_select(
                        candidates, active_trades, close_df, i,
                        min(effective_top_k, slots_available))
                else:
                    selected = candidates[:min(effective_top_k, slots_available)]

                # 相關性過濾：去除與已選股/持倉高度相關的候選
                # Dynamic correlation filter: 強勢 regime 放寬閾值
                eff_corr_filter = self.corr_filter
                if self.dynamic_corr_filter and self.corr_filter > 0:
                    if regime_scale >= 1.0:
                        eff_corr_filter = min(0.85, self.corr_filter + 0.05)
                    elif regime_scale >= 0.7:
                        eff_corr_filter = self.corr_filter  # 維持原值
                    else:
                        eff_corr_filter = max(0.6, self.corr_filter - 0.1)

                if eff_corr_filter > 0 and len(selected) > 1:
                    try:
                        lookback = min(20, i)
                        if lookback >= 10:
                            sel_tickers = [s[0] for s in selected]
                            all_held = list(active_trades.keys()) + sel_tickers
                            ret_slice = close_df[all_held].iloc[max(0,i-lookback):i].pct_change().dropna()
                            if len(ret_slice) >= 5:
                                corr = ret_slice.corr()
                                to_drop = set()
                                for si in range(len(sel_tickers)):
                                    if sel_tickers[si] in to_drop:
                                        continue
                                    for sj in range(si+1, len(sel_tickers)):
                                        pair_corr = corr.loc[sel_tickers[si], sel_tickers[sj]] \
                                            if sel_tickers[si] in corr.index and sel_tickers[sj] in corr.columns \
                                            else 0
                                        if pair_corr > eff_corr_filter:
                                            to_drop.add(sel_tickers[sj])
                                if to_drop:
                                    selected = [s for s in selected if s[0] not in to_drop]
                                    # 補上被過濾掉的名額
                                    remaining = [c for c in candidates if c[0] not in
                                                 {s[0] for s in selected} and c[0] not in to_drop]
                                    selected += remaining[:min(top_k, slots_available) - len(selected)]
                    except Exception:
                        pass

                for rank_idx, (ticker, score, entry_price) in enumerate(selected):
                    # === Portfolio Heat Cap: 進場前檢查組合總風險 ===
                    if self.max_portfolio_heat < 1.0 and active_trades:
                        heat = 0
                        for t_ticker, t_trade in active_trades.items():
                            t_price = close_df[t_ticker].iloc[i] if not pd.isna(close_df[t_ticker].iloc[i]) else t_trade['entry_price']
                            risk_per_share = max(0, t_price - t_trade['sl_price'])
                            heat += t_trade['shares'] * risk_per_share
                        heat_pct = heat / current_equity if current_equity > 0 else 0
                        if heat_pct >= self.max_portfolio_heat:
                            continue  # 組合熱度已滿，跳過新進場

                    # 滑價模型：買入時價格略高
                    actual_entry = entry_price * (1 + self.slippage)

                    # === Gap-aware sizing：跳空越大，倉位越小 ===
                    gap_scale = 1.0
                    if self.gap_aware_sizing and atr is not None:
                        prev_close_val = close_df[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        atr_val_gap = atr[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        if not pd.isna(prev_close_val) and not pd.isna(atr_val_gap) and atr_val_gap > 0:
                            gap_atr = abs(entry_price - prev_close_val) / atr_val_gap
                            if gap_atr >= 1.0:
                                gap_scale = 0.5   # 大跳空：半倉
                            elif gap_atr >= 0.5:
                                gap_scale = 0.75  # 中跳空：3/4 倉

                    # === 排名加權 sizing ===
                    rank_weight = 1.0
                    if self.rank_weighted and len(selected) > 1:
                        raw_weights = [1.4 - 0.2 * j for j in range(len(selected))]
                        total_w = sum(raw_weights)
                        rank_weight = raw_weights[rank_idx] / total_w * len(selected)

                    # === 動態風險預算：根據近期 realized vol 調整 position size ===
                    # Batch entry is intentionally disabled at the CLI until
                    # pending-order execution is modeled end to end.
                    batch_scale = 1.0
                    if self.batch_entry > 1:
                        # 剩餘批次由後續幾天的 pending_batches 自動追蹤
                        weights = {2: [0.55, 0.45], 3: [0.45, 0.30, 0.25]}
                        batch_scale = weights.get(self.batch_entry, [1.0/self.batch_entry]*self.batch_entry)[0]

                    book = 'core' if ticker in self.core_tickers else 'satellite'
                    if (self.hybrid_tiered and book == 'satellite'
                            and (self._daily_rotation or {}).get('sat_entry_freeze', 0) >= 1.0):
                        continue

                    effective_pos_size = self.position_size * rank_weight * regime_scale * gap_scale * batch_scale

                    # === v9 hybrid：平常滿倉，不因 VIX 縮 Satellite ===
                    vix_trade_mult = 1.0
                    if self._vix_series is not None and i > 0 and not self.hybrid_tiered:
                        try:
                            prev_date = dates[i - 1]
                            vix_idx = self._vix_series.index.get_indexer([prev_date], method='ffill')[0]
                            if vix_idx >= 0:
                                vix_val = float(self._vix_series.iloc[vix_idx])
                                if vix_val > 33:
                                    vix_trade_mult = 0.8
                                elif vix_val > 28:
                                    vix_trade_mult = 0.0
                        except Exception:
                            pass

                    rotation_boost = 1.0
                    tiered_scale = 1.0
                    if self.hybrid_tiered:
                        rot = self._daily_rotation or {}
                        if book == 'core':
                            rotation_boost = rot.get('core_rotation_boost', 1.0)
                        else:
                            rotation_boost = rot.get('sat_rotation_boost', 1.0)
                        self._tiered_scales_log.append({
                            'date': str(date), 'book': book,
                            'scale': round(rotation_boost, 4),
                            'tiered_scale': 1.0,
                            'rotation_boost': round(rotation_boost, 4),
                            'vol_regime': rot.get('vol_regime', 'normal'),
                            'fvol': round(self._last_fvol or 0, 4),
                            'vix_mult': round(vix_trade_mult, 4),
                        })

                    effective_pos_size = effective_pos_size * vix_trade_mult * rotation_boost

                    if self.dynamic_risk and market_daily_ret is not None:
                        try:
                            prev_date = dates[i - 1]
                            mkt_idx = market_close.index.get_indexer([prev_date], method='ffill')[0]
                            if mkt_idx >= 20:
                                recent_vol = market_daily_ret.iloc[mkt_idx-20:mkt_idx].std()
                                target_vol = 0.01  # 目標日波動 1%
                                if not pd.isna(recent_vol) and recent_vol > 0:
                                    vol_scalar = min(2.0, max(0.3, target_vol / recent_vol))
                                    effective_pos_size = effective_pos_size * vol_scalar
                        except Exception:
                            pass

                    # Volatility Parity 或 固定/動態比例 sizing
                    if self.vol_parity and atr is not None:
                        atr_val_sizing = atr[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                        if not pd.isna(atr_val_sizing) and atr_val_sizing > 0:
                            target_risk = current_equity * effective_pos_size
                            risk_per_share = atr_val_sizing * self.sl_atr_mult
                            shares = target_risk / risk_per_share if risk_per_share > 0 else 0
                            trade_amount = shares * actual_entry
                        else:
                            trade_amount = current_equity * effective_pos_size
                    else:
                        trade_amount = current_equity * effective_pos_size

                    actual_cost = trade_amount * (1 + self.buy_cost)  # 含買入手續費

                    if capital >= actual_cost:
                        shares = trade_amount / actual_entry

                        # 計算 TP/SL 價格（基於實際進場價含滑價）
                        atr_val = np.nan
                        if self.tp_sl_mode == 'atr' and atr is not None:
                            atr_val = atr[ticker].iloc[i - 1] if i - 1 >= 0 else np.nan
                            if pd.isna(atr_val) or atr_val <= 0:
                                # fallback 到固定百分比
                                tp_price = actual_entry * (1 + self.tp_pct)
                                sl_price = actual_entry * (1 - self.sl_pct)
                            else:
                                tp_price = actual_entry + atr_val * self.tp_atr_mult
                                sl_price = actual_entry - atr_val * self.sl_atr_mult
                        else:
                            tp_price = actual_entry * (1 + self.tp_pct)
                            sl_price = actual_entry * (1 - self.sl_pct)

                        reject_entry, reject_info = self._low_wr_rr_reject(
                            ticker, ticker_history, tp_price, sl_price, actual_entry,
                        )
                        if reject_entry:
                            self._rejected_entry_log.append({
                                'date': str(date),
                                'ticker': ticker,
                                'reason': 'low_wr_rr_gate',
                                **reject_info,
                            })
                            continue

                        capital -= actual_cost

                        active_trades[ticker] = {
                            'shares': shares,
                            'entry_price': actual_entry,
                            'entry_date': date,
                            'tp_price': tp_price,
                            'sl_price': sl_price,
                            'initial_sl_price': sl_price,
                            'highest_since_entry': actual_entry,
                            'breakeven_activated': False,
                            'atr_at_entry': atr_val if (self.tp_sl_mode == 'atr'
                                                        and atr is not None
                                                        and not pd.isna(atr_val)) else 0,
                            'days_held': 0,
                            'actual_cost': actual_cost,
                            'book': book,  # v9
                            'tiered_scale': round(tiered_scale, 4),  # v9
                        }

            # ── Step 4: 結算今日總權益（現金 + 所有持倉市值） ──
            today_equity = capital
            for ticker, trade in active_trades.items():
                close_val = close_df[ticker].iloc[i]
                if not pd.isna(close_val):
                    today_equity += trade['shares'] * close_val

            equity_curve.append({'Date': date, 'Equity': today_equity})

        equity_df = pd.DataFrame(equity_curve).set_index('Date')
        trades_df = pd.DataFrame(trades)

        # 輸出回測摘要
        if not trades_df.empty:
            wins = len(trades_df[trades_df['Return_Pct'] > 0])
            total = len(trades_df)
            total_cost_impact = (self.buy_cost + self.sell_cost) * total
            summary = (f"   ✅ 回測完成：共 {total} 筆交易，"
                       f"勝率 {wins/total*100:.1f}%，"
                       f"平均報酬 {trades_df['Return_Pct'].mean()*100:.2f}% "
                       f"(含成本 ~{(self.buy_cost+self.sell_cost)*100:.2f}%/筆)")
            if self.futures_hedge and hedge_pnl_total != 0:
                summary += f" [期貨對沖損益: {hedge_pnl_total:+,.0f}]"
            print(summary)
        else:
            print("   ⚠️  回測完成但無任何交易觸發")

        return trades_df, equity_df
