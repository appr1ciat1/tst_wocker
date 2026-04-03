"""
事件驅動回測引擎 (Event-Driven Backtest Engine)

支援三種出場機制：
1. 區間停利 (Take Profit, TP) — 盤中最高價觸碰目標價即出場
2. 絕對停損 (Stop Loss, SL) — 盤中最低價跌破防守價即砍倉
3. 時間強制出場 (Time Exit) — 持有滿 N 個交易日強制以收盤價平倉

核心特色：
- 使用每日 High/Low 進行精確觸價回測（非僅收盤價），貼近實戰
- 停損優先判定（保守原則：同一天同時觸碰 TP 和 SL 時，以 SL 計算）
- 與策略完全解耦：只需給它「分數矩陣 + OHLC 矩陣」就能運行
"""

import pandas as pd


class EventDrivenBacktester:
    """
    事件驅動回測器。

    Parameters
    ----------
    tp_pct : float
        停利百分比，例如 0.15 代表 +15%
    sl_pct : float
        停損百分比，例如 0.08 代表 -8%
    max_hold_days : int
        最大持倉交易日數
    initial_capital : float
        初始模擬資金
    position_size : float
        每次進場佔初始資金的比例（例如 0.10 = 10%）
    """

    def __init__(self, tp_pct=0.15, sl_pct=0.08, max_hold_days=20,
                 initial_capital=1_000_000, position_size=0.10):
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold_days = max_hold_days
        self.initial_capital = initial_capital
        self.position_size = position_size

    def run(self, total_score, close_df, high_df, low_df, ma_60, threshold=3.2):
        """
        執行事件驅動回測。

        Parameters
        ----------
        total_score : pd.DataFrame
            AI 綜合評分矩陣 (日期 x 股票)
        close_df : pd.DataFrame
            收盤價矩陣
        high_df : pd.DataFrame
            最高價矩陣
        low_df : pd.DataFrame
            最低價矩陣
        ma_60 : pd.DataFrame
            60 日均線矩陣（進場過濾條件）
        threshold : float
            進場信號門檻（score >= threshold 且 close > MA60）

        Returns
        -------
        trades_df : pd.DataFrame
            所有已完成交易的明細
        equity_df : pd.DataFrame
            每日資金曲線
        """
        print(f"💰 執行精準區間回測 (停利 +{self.tp_pct*100:.0f}%, "
              f"停損 -{self.sl_pct*100:.0f}%, "
              f"最長持有 {self.max_hold_days} 天)...")

        trades = []
        capital = self.initial_capital
        equity_curve = []
        dates = close_df.index
        active_trades = {}  # ticker -> trade_info

        # 從第 60 天開始（確保技術指標已穩定）
        for i in range(60, len(dates)):
            date = dates[i]

            # ── Step 1: 處理持倉的出場判定（根據今日盤中高低價） ──
            exited_tickers = []
            for ticker, trade in active_trades.items():
                trade['days_held'] += 1

                current_high = high_df[ticker].iloc[i]
                current_low = low_df[ticker].iloc[i]
                current_close = close_df[ticker].iloc[i]

                if pd.isna(current_close):
                    continue

                exit_triggered = False
                exit_price = 0
                exit_reason = ""

                # 優先檢查停損（保守回測法，確保風險不被低估）
                if current_low <= trade['sl_price']:
                    exit_triggered = True
                    exit_price = trade['sl_price']
                    exit_reason = "🔴 停損"
                elif current_high >= trade['tp_price']:
                    exit_triggered = True
                    exit_price = trade['tp_price']
                    exit_reason = "🟢 停利"
                elif trade['days_held'] >= self.max_hold_days:
                    exit_triggered = True
                    exit_price = current_close
                    exit_reason = "⚪ 時間到期"

                if exit_triggered:
                    revenue = trade['shares'] * exit_price
                    capital += revenue

                    profit_pct = (exit_price - trade['entry_price']) / trade['entry_price']
                    trades.append({
                        'Ticker': ticker,
                        'Entry_Date': trade['entry_date'].strftime('%Y-%m-%d'),
                        'Exit_Date': date.strftime('%Y-%m-%d'),
                        'Entry_Price': round(trade['entry_price'], 2),
                        'Exit_Price': round(exit_price, 2),
                        'Return_Pct': round(profit_pct, 4),
                        'Reason': exit_reason,
                        'Days_Held': trade['days_held'],
                    })
                    exited_tickers.append(ticker)

            # 移除已出場的股票
            for t in exited_tickers:
                del active_trades[t]

            # ── Step 2: 處理今日進場（根據昨日收盤信號） ──
            for ticker in close_df.columns:
                score = total_score[ticker].iloc[i - 1]
                ma = ma_60[ticker].iloc[i - 1]
                current_close = close_df[ticker].iloc[i]

                if pd.isna(current_close) or pd.isna(score) or pd.isna(ma):
                    continue

                # 進場條件：
                # 1. 尚未持有該股
                # 2. 昨日 AI 評分 >= 門檻
                # 3. 昨日收盤價 > 60MA（多頭趨勢確認）
                if (ticker not in active_trades
                        and score >= threshold
                        and close_df[ticker].iloc[i - 1] > ma):

                    trade_amount = self.initial_capital * self.position_size
                    if capital >= trade_amount:
                        shares = trade_amount / current_close
                        capital -= trade_amount

                        active_trades[ticker] = {
                            'shares': shares,
                            'entry_price': current_close,
                            'entry_date': date,
                            'tp_price': current_close * (1 + self.tp_pct),
                            'sl_price': current_close * (1 - self.sl_pct),
                            'days_held': 0,
                        }

            # ── Step 3: 結算今日總權益（現金 + 所有持倉市值） ──
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
            print(f"   ✅ 回測完成：共 {total} 筆交易，"
                  f"勝率 {wins/total*100:.1f}%，"
                  f"平均報酬 {trades_df['Return_Pct'].mean()*100:.2f}%")
        else:
            print("   ⚠️  回測完成但無任何交易觸發")

        return trades_df, equity_df
