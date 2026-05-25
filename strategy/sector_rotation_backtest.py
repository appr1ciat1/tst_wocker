"""
板塊輪動回測引擎 (Sector Rotation Backtester)

完全獨立於 v8.5 的 EventDrivenBacktester。
使用美股 Macro Regime (SPY/VIX/SOX) → 板塊資金流選擇 → 板塊內選股
的三層架構。

出場邏輯沿用 ATR TP/SL + 最大持倉天數。

v1.2 — 2026-04-09  (beat-00981A: dynamic slots + tech boost + trend hold)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


class SectorRotationBacktester:
    """
    板塊輪動策略回測引擎。

    Three-Layer Architecture:
    1. Macro Regime (US): SPY/VIX → overall exposure, SOX → tech gate
    2. Sector Flow: 10/15/20d sector momentum → select top sectors
    3. Intra-sector: momentum ranking within selected sectors → pick stocks
    """

    def __init__(
        self,
        initial_capital=1_000_000,
        position_size=0.10,
        tp_atr_mult=4.0,
        sl_atr_mult=3.0,
        max_hold_days=20,
        max_extend_days=10,
        top_sectors=3,
        stocks_per_sector=3,
        flow_windows=None,
        flow_weights=None,
        gap_filter_atr=1.5,
        sector_min_flow=0.0,
        buy_cost=0.001425,
        sell_cost=0.004425,
        slippage=0.001,
    ):
        self.initial_capital = initial_capital
        self.position_size = position_size
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.max_hold_days = max_hold_days
        self.max_extend_days = max_extend_days
        self.top_sectors = top_sectors
        self.stocks_per_sector = stocks_per_sector
        self.flow_windows = flow_windows or [10, 15, 20]
        self.flow_weights = flow_weights or [0.25, 0.50, 0.25]
        self.gap_filter_atr = gap_filter_atr
        self.sector_min_flow = sector_min_flow
        self.buy_cost = buy_cost
        self.sell_cost = sell_cost
        self.slippage = slippage

    def _compute_atr(self, high_df, low_df, close_df, period=14):
        """計算 ATR。"""
        tr1 = high_df - low_df
        tr2 = (high_df - close_df.shift(1)).abs()
        tr3 = (low_df - close_df.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=0).groupby(level=0).max()
        tr = tr.reindex(close_df.index)
        return tr.rolling(period).mean()

    def run(self, close_df, open_df, high_df, low_df, vol_df,
            us_signals_aligned, universe_mask=None):
        """
        執行板塊輪動回測。

        Parameters
        ----------
        close_df, open_df, high_df, low_df, vol_df : pd.DataFrame
            台股 OHLCV 數據
        us_signals_aligned : pd.DataFrame
            已對齊到台股交易日的美股信號 (shift(1) applied)
        universe_mask : pd.DataFrame (bool), optional
            流動性 Universe 遮罩

        Returns
        -------
        trades_df : pd.DataFrame
            交易紀錄
        equity_df : pd.DataFrame
            每日權益曲線
        """
        from strategy.sector_flow import classify_sector, compute_sector_flow

        atr = self._compute_atr(high_df, low_df, close_df)

        # 預計算板塊資金流
        sector_flow_df, sector_tickers = compute_sector_flow(
            close_df, universe_mask,
            windows=self.flow_windows,
            weights=self.flow_weights,
        )
        print(f"   📊 板塊輪動: {len(sector_tickers)} 板塊, "
              f"窗口={self.flow_windows}")

        # 計算個股 20d momentum 用於板塊內排名
        mom_20d = close_df / close_df.shift(20) - 1
        ma_60 = close_df.rolling(60).mean()
        ma_20 = close_df.rolling(20).mean()

        dates = close_df.index
        trades = []
        capital = self.initial_capital
        equity_curve = []
        active_trades = {}
        max_positions = int(1.0 / self.position_size)

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

        # 統計
        regime_stats = {'full': 0, 'reduced': 0, 'minimal': 0, 'stopped': 0}
        tech_gate_stats = {'open': 0, 'half': 0, 'closed': 0}
        sector_entry_counts = {}

        for i in range(60, len(dates)):
            date = dates[i]

            # ── Step 1: 處理出場 ──
            exited_tickers = []
            for ticker, trade in active_trades.items():
                trade['days_held'] += 1
                if not is_tradable_bar(ticker, i):
                    continue
                current_high = high_df[ticker].iloc[i]
                current_low = low_df[ticker].iloc[i]
                current_close = close_df[ticker].iloc[i]
                current_open = open_df[ticker].iloc[i] if ticker in open_df.columns else np.nan

                if pd.isna(current_close):
                    continue

                exit_triggered = False
                exit_price = 0
                exit_reason = ""

                # 停損 (gap-aware)
                if current_low <= trade['sl_price']:
                    exit_triggered = True
                    if not pd.isna(current_open) and current_open < trade['sl_price']:
                        exit_price = current_open
                    else:
                        exit_price = trade['sl_price']
                    exit_reason = "🔴 停損"
                # 停利 (gap-aware)
                elif current_high >= trade['tp_price']:
                    exit_triggered = True
                    if not pd.isna(current_open) and current_open > trade['tp_price']:
                        exit_price = current_open
                    else:
                        exit_price = trade['tp_price']
                    exit_reason = "🟢 停利"
                # 時間到期（含趨勢延續邏輯）
                elif trade['days_held'] >= self.max_hold_days:
                    # ★ 趨勢延續: 板塊仍在 top 3 + 股價 > MA20 → 續持
                    max_total = self.max_hold_days + self.max_extend_days
                    can_extend = (
                        trade['days_held'] < max_total
                        and not trade.get('extended', False)
                    )

                    if can_extend:
                        # 檢查板塊是否仍強
                        sector = trade.get('sector', '')
                        sector_still_strong = False
                        if i - 1 < len(sector_flow_df):
                            day_flow = sector_flow_df.iloc[i - 1].dropna()
                            if len(day_flow) > 0:
                                top_sectors_now = day_flow.nlargest(
                                    self.top_sectors).index.tolist()
                                sector_still_strong = sector in top_sectors_now

                        # 檢查股價是否 > MA20；用 t-1 避免收盤後訊號同日決策。
                        ma20_val = ma_20[ticker].iloc[i - 1] if ticker in ma_20.columns else np.nan
                        prev_close = close_df[ticker].iloc[i - 1] if ticker in close_df.columns else np.nan
                        stock_above_ma20 = (
                            not pd.isna(ma20_val)
                            and not pd.isna(prev_close)
                            and prev_close > ma20_val
                        )

                        if sector_still_strong and stock_above_ma20:
                            trade['extended'] = True
                            continue  # 續持，不出場

                    exit_triggered = True
                    exit_price = current_close
                    exit_reason = "⚪ 時間到期"

                if exit_triggered:
                    exit_price_adj = exit_price * (1 - self.slippage)
                    revenue = trade['shares'] * exit_price_adj * (1 - self.sell_cost)
                    capital += revenue
                    profit_pct = (revenue - trade['actual_cost']) / trade['actual_cost']

                    trades.append({
                        'Ticker': ticker,
                        'Sector': trade.get('sector', ''),
                        'Entry_Date': trade['entry_date'].strftime('%Y-%m-%d'),
                        'Exit_Date': date.strftime('%Y-%m-%d'),
                        'Entry_Price': round(trade['entry_price'], 2),
                        'Exit_Price': round(exit_price, 2),
                        'Return_Pct': round(profit_pct, 4),
                        'Reason': exit_reason,
                        'Days_Held': trade['days_held'],
                        'Regime': trade.get('regime_at_entry', 1.0),
                        'Tech_Gate': trade.get('tech_gate_at_entry', 'open'),
                    })
                    exited_tickers.append(ticker)

            for t in exited_tickers:
                del active_trades[t]

            # ── Step 2: 計算當前權益 ──
            current_equity = capital
            for ticker, trade in active_trades.items():
                close_val = close_df[ticker].iloc[i]
                if not pd.isna(close_val):
                    current_equity += trade['shares'] * close_val
            equity_curve.append({'Date': date, 'Equity': current_equity})

            # ── Step 3: 進場決策 ──
            slots_available = max_positions - len(active_trades)
            if slots_available <= 0:
                continue

            # === Layer 1: Macro Regime (美股前提) ===
            macro_regime = 0.4  # default
            tech_gate = 'open'  # default

            if us_signals_aligned is not None and i < len(us_signals_aligned):
                try:
                    day_us = us_signals_aligned.iloc[i]
                    if not pd.isna(day_us.get('macro_regime', np.nan)):
                        macro_regime = day_us['macro_regime']
                    if pd.notna(day_us.get('tech_gate', None)):
                        tech_gate = day_us['tech_gate']
                except (IndexError, KeyError):
                    pass

            # 統計
            if macro_regime >= 0.9:
                regime_stats['full'] += 1
            elif macro_regime >= 0.5:
                regime_stats['reduced'] += 1
            elif macro_regime > 0:
                regime_stats['minimal'] += 1
            else:
                regime_stats['stopped'] += 1

            tech_gate_stats[tech_gate] = tech_gate_stats.get(tech_gate, 0) + 1

            # Macro regime 停止 → 不進場
            if macro_regime <= 0:
                continue

            # 根據 regime 調整有效 slot 數
            effective_slots = max(1, int(slots_available * macro_regime))

            # === Layer 2: 板塊資金流排名 ===
            if i - 1 < 0 or i - 1 >= len(sector_flow_df):
                continue

            day_sector_flow = sector_flow_df.iloc[i - 1]  # t-1 數據
            valid_flows = day_sector_flow.dropna()

            if len(valid_flows) == 0:
                continue

            # 全部板塊都是負的 → 不進場
            if (valid_flows <= self.sector_min_flow).all():
                continue

            # ★ 板塊動量地板：所有板塊 20d 平均回報 < -3% → 不進場
            # (全面下跌時「跌最少的板塊」仍在跌)
            sector_avg_flow = valid_flows.mean()
            if sector_avg_flow < -0.03:
                continue

            # 排名取前 N 板塊
            ranked_sectors = valid_flows.sort_values(ascending=False)

            selected_sectors = []
            tech_sectors = {'semiconductor', 'electronics', 'computing'}

            for sector_name, flow_score in ranked_sectors.items():
                if len(selected_sectors) >= self.top_sectors:
                    break

                # Tech gate 檢查
                if sector_name in tech_sectors:
                    if tech_gate == 'closed':
                        continue  # SOX 禁止 → 跳過科技板塊
                    # tech_gate == 'all_half' / 'half' → 可以進但後面會減倉

                selected_sectors.append((sector_name, flow_score))

            if not selected_sectors:
                continue

            # ★ 動態 Slot 配比：按 flow 強度加權分配
            total_flow = sum(max(0, fs) for _, fs in selected_sectors)
            if total_flow > 0:
                sector_slot_map = {}
                total_slots = effective_slots
                for sn, fs in selected_sectors:
                    weight = max(0, fs) / total_flow
                    sector_slot_map[sn] = max(1, round(weight * total_slots))
            else:
                # 全部 flow 都 <= 0 → 均分
                per = max(1, effective_slots // len(selected_sectors))
                sector_slot_map = {sn: per for sn, _ in selected_sectors}

            # === Layer 3: 板塊內選股 ===
            candidates = []
            for sector_name, flow_score in selected_sectors:
                sector_stocks = sector_tickers.get(sector_name, [])

                # 過濾 Universe 內的股票
                valid_stocks = []
                for ticker in sector_stocks:
                    if ticker not in close_df.columns:
                        continue
                    if universe_mask is not None:
                        if ticker not in universe_mask.columns:
                            continue
                        if not universe_mask[ticker].iloc[i - 1]:
                            continue
                    if not is_tradable_bar(ticker, i):
                        continue

                    close_val = close_df[ticker].iloc[i - 1]
                    if pd.isna(close_val) or close_val <= 0:
                        continue

                    # 跳過已持倉
                    if ticker in active_trades:
                        continue

                    # 板塊內評分：momentum(20d) × 2 + trend(close > MA60) × 1
                    mom = mom_20d[ticker].iloc[i - 1] if ticker in mom_20d.columns else np.nan
                    ma60_val = ma_60[ticker].iloc[i - 1] if ticker in ma_60.columns else np.nan

                    if pd.isna(mom):
                        continue

                    trend_score = 1.0 if (not pd.isna(ma60_val) and close_val > ma60_val) else 0.0
                    intra_score = mom * 2 + trend_score

                    valid_stocks.append((ticker, intra_score, close_val, sector_name))

                # 板塊內排名
                valid_stocks.sort(key=lambda x: x[1], reverse=True)

                # ★ 動態 slot: 用 flow 加權的 slot 數
                n_pick = sector_slot_map.get(sector_name, self.stocks_per_sector)

                # Tech gate 加碼/減倉
                if tech_gate == 'boost' and sector_name in tech_sectors:
                    # SOX 超強: 科技板塊 slot ×1.5
                    n_pick = max(1, int(n_pick * 1.5))
                elif tech_gate == 'strong' and sector_name in tech_sectors:
                    # SOX 強勢: 科技板塊 slot ×1.2
                    n_pick = max(1, int(n_pick * 1.2))
                elif tech_gate == 'all_half':
                    # SOX < MA60: 所有板塊都減半
                    n_pick = max(1, n_pick // 2)
                elif tech_gate == 'closed':
                    # SOX 崩跌: 科技已被擋，其他板塊也減半
                    if sector_name not in tech_sectors:
                        n_pick = max(1, n_pick // 2)
                elif sector_name in tech_sectors and tech_gate in ('half',):
                    n_pick = max(1, n_pick // 2)

                candidates.extend(valid_stocks[:n_pick])

            # 按板塊內分數排序，取 effective_slots 名
            candidates.sort(key=lambda x: x[1], reverse=True)
            selected = candidates[:effective_slots]

            # === 進場 ===
            for ticker, score, entry_price_prev, sector in selected:
                if len(active_trades) >= max_positions:
                    break

                entry_price = open_df[ticker].iloc[i] if ticker in open_df.columns else close_df[ticker].iloc[i]
                if pd.isna(entry_price) or entry_price <= 0:
                    continue

                # Gap filter
                prev_close = close_df[ticker].iloc[i - 1]
                ticker_atr = atr[ticker].iloc[i - 1] if ticker in atr.columns else np.nan
                if not pd.isna(prev_close) and not pd.isna(ticker_atr) and ticker_atr > 0:
                    gap = abs(entry_price - prev_close)
                    if gap > self.gap_filter_atr * ticker_atr:
                        continue

                # ATR TP/SL
                if not pd.isna(ticker_atr) and ticker_atr > 0:
                    tp_price = entry_price + ticker_atr * self.tp_atr_mult
                    sl_price = entry_price - ticker_atr * self.sl_atr_mult
                else:
                    tp_price = entry_price * 1.10
                    sl_price = entry_price * 0.90

                # 計算倉位
                position_value = current_equity * self.position_size
                buy_price_adj = entry_price * (1 + self.slippage)
                shares = int(position_value / (buy_price_adj * (1 + self.buy_cost)))
                if shares <= 0:
                    continue

                actual_cost = shares * buy_price_adj * (1 + self.buy_cost)
                if actual_cost > capital:
                    continue

                capital -= actual_cost

                active_trades[ticker] = {
                    'entry_date': date,
                    'entry_price': entry_price,
                    'shares': shares,
                    'actual_cost': actual_cost,
                    'tp_price': tp_price,
                    'sl_price': sl_price,
                    'days_held': 0,
                    'sector': sector,
                    'regime_at_entry': macro_regime,
                    'tech_gate_at_entry': tech_gate,
                }

                # 統計板塊進場次數
                sector_entry_counts[sector] = sector_entry_counts.get(sector, 0) + 1

        # 最終平倉
        final_date = dates[-1] if len(dates) > 0 else datetime.today()
        for ticker, trade in active_trades.items():
            close_val = close_df[ticker].iloc[-1]
            if not pd.isna(close_val):
                exit_price_adj = close_val * (1 - self.slippage)
                revenue = trade['shares'] * exit_price_adj * (1 - self.sell_cost)
                capital += revenue
                profit_pct = (revenue - trade['actual_cost']) / trade['actual_cost']
                trades.append({
                    'Ticker': ticker,
                    'Sector': trade.get('sector', ''),
                    'Entry_Date': trade['entry_date'].strftime('%Y-%m-%d'),
                    'Exit_Date': final_date.strftime('%Y-%m-%d'),
                    'Entry_Price': round(trade['entry_price'], 2),
                    'Exit_Price': round(close_val, 2),
                    'Return_Pct': round(profit_pct, 4),
                    'Reason': '⚪ 回測結束平倉',
                    'Days_Held': trade['days_held'],
                    'Regime': trade.get('regime_at_entry', 1.0),
                    'Tech_Gate': trade.get('tech_gate_at_entry', 'open'),
                })

        trades_df = pd.DataFrame(trades)
        equity_df = pd.DataFrame(equity_curve)

        if not equity_df.empty:
            equity_df.set_index('Date', inplace=True)

        # 印出統計
        print(f"\n   📊 Macro Regime 分布:")
        total_days = sum(regime_stats.values())
        for k, v in regime_stats.items():
            pct = v / total_days * 100 if total_days > 0 else 0
            print(f"      {k}: {v} 天 ({pct:.0f}%)")

        print(f"   🔧 Tech Gate 分布:")
        for k, v in tech_gate_stats.items():
            pct = v / total_days * 100 if total_days > 0 else 0
            print(f"      {k}: {v} 天 ({pct:.0f}%)")

        if sector_entry_counts:
            print(f"   📈 板塊進場次數:")
            for sector, count in sorted(sector_entry_counts.items(),
                                        key=lambda x: x[1], reverse=True):
                print(f"      {sector}: {count}")

        return trades_df, equity_df
