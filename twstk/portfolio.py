"""
twstk.portfolio — 目標權重執行核心（回測 + 每日模擬 共用）

策略只負責產生「每日目標權重」(target_weights)；這裡負責把權重轉成實際買賣：

  - 用「前一交易日」決定的權重，於「當日開盤」換倉（避免前視）。
  - 計算買賣價差、手續費 / 證交稅、滑價。
  - 收盤 mark-to-market，累積權益曲線。
  - 狀態以純 dict 表示，可序列化成 JSON（供每日模擬增量續跑 / 重播）。

回測與每日模擬使用「完全相同」的成交語意，因此兩者結果可比較、策略可互通。
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PortfolioConfig:
    initial_capital: float = 1_000_000
    buy_cost: float = 0.001425          # 買進手續費
    sell_cost: float = 0.004425         # 賣出手續費 + 證交稅
    slippage: float = 0.0               # 單邊滑價（0.001 = 10bps）
    max_weight: float = 1.0             # 單檔權重上限
    rebalance_threshold: float = 0.0    # 權重變動小於此值不換倉（降週轉）


def new_state(start_date, initial_capital):
    """建立空白組合狀態（JSON 可序列化）。"""
    return {
        "start_date": str(start_date),
        "last_date": None,
        "initial_capital": float(initial_capital),
        "cash": float(initial_capital),
        "positions": {},               # {ticker: shares(int)}
        "equity_curve": [],            # [{date, equity}]
        "trades": [],                  # [{date, ticker, side, shares, price, value, cost}]
    }


def _price(df, d, t):
    try:
        v = df.at[d, t]
        return float(v) if pd.notna(v) else np.nan
    except (KeyError, IndexError):
        return np.nan


def simulate_weights(weights: pd.DataFrame,
                     open_df: pd.DataFrame,
                     close_df: pd.DataFrame,
                     cfg: PortfolioConfig,
                     state: Optional[dict] = None,
                     start: Optional[str] = None,
                     end: Optional[str] = None) -> dict:
    """
    依目標權重逐日模擬，回傳更新後的 state。

    Parameters
    ----------
    weights : (日期 × 代號) 目標權重；每列為當日想持有的權重
    open_df, close_df : 開盤 / 收盤價矩陣（用於成交與 MTM）
    cfg : PortfolioConfig
    state : 既有狀態（增量續跑用）；None 代表全新開始
    start, end : 模擬區間（含端點）；None 則用 close_df 全區間
    """
    if state is None:
        state = new_state(start or close_df.index[0], cfg.initial_capital)

    lo = pd.Timestamp(start) if start else close_df.index[0]
    hi = pd.Timestamp(end) if end else close_df.index[-1]
    dates = [d for d in close_df.index if lo <= d <= hi]
    if state.get("last_date"):
        last = pd.Timestamp(state["last_date"])
        dates = [d for d in dates if d > last]

    widx = weights.index
    positions = state["positions"]

    for d in dates:
        # 訊號來自「嚴格早於 d」的最後一個權重列（前一日決策、今日開盤成交）
        prior = widx[widx < d]
        target_row = weights.loc[prior[-1]] if len(prior) else None

        # 以開盤價估當前權益（換倉基準）
        def open_or_close(t):
            p = _price(open_df, d, t)
            return p if not np.isnan(p) else _price(close_df, d, t)

        holdings_val = 0.0
        for t, sh in positions.items():
            p = open_or_close(t)
            if not np.isnan(p):
                holdings_val += sh * p
        equity_open = state["cash"] + holdings_val

        if target_row is not None and equity_open > 0:
            tgt = target_row.dropna()
            tickers = set(positions) | set(tgt.index[tgt > 0])

            # 算出每檔目標股數
            desired = {}
            for t in tickers:
                p = open_or_close(t)
                if np.isnan(p) or p <= 0:
                    desired[t] = positions.get(t, 0)   # 無價→維持
                    continue
                w = float(tgt.get(t, 0.0))
                w = min(max(w, 0.0), cfg.max_weight)
                # rebalance 門檻：權重變動太小就不動
                cur_w = (positions.get(t, 0) * p) / equity_open if equity_open else 0.0
                if cfg.rebalance_threshold and abs(w - cur_w) < cfg.rebalance_threshold:
                    desired[t] = positions.get(t, 0)
                    continue
                desired[t] = int((equity_open * w) // p)

            # 先賣（釋放現金）再買
            for t in list(tickers):
                cur = positions.get(t, 0)
                diff = desired[t] - cur
                if diff < 0:
                    p = open_or_close(t)
                    qty = -diff
                    proceeds = qty * p * (1 - cfg.sell_cost - cfg.slippage)
                    state["cash"] += proceeds
                    new_sh = cur - qty
                    if new_sh > 0:
                        positions[t] = new_sh
                    else:
                        positions.pop(t, None)
                    state["trades"].append({
                        "date": str(d.date()), "ticker": t, "side": "SELL",
                        "shares": qty, "price": round(p, 3),
                        "value": round(proceeds, 2),
                    })
            for t in list(tickers):
                cur = positions.get(t, 0)
                diff = desired[t] - cur
                if diff > 0:
                    p = open_or_close(t)
                    cost = diff * p * (1 + cfg.buy_cost + cfg.slippage)
                    if cost > state["cash"]:
                        # 現金不足：買能買的
                        affordable = int(state["cash"] // (p * (1 + cfg.buy_cost + cfg.slippage)))
                        diff = max(affordable, 0)
                        cost = diff * p * (1 + cfg.buy_cost + cfg.slippage)
                    if diff <= 0:
                        continue
                    state["cash"] -= cost
                    positions[t] = cur + diff
                    state["trades"].append({
                        "date": str(d.date()), "ticker": t, "side": "BUY",
                        "shares": diff, "price": round(p, 3),
                        "value": round(cost, 2),
                    })

        # 收盤 mark-to-market
        mtm = 0.0
        for t, sh in positions.items():
            p = _price(close_df, d, t)
            if not np.isnan(p):
                mtm += sh * p
        equity = state["cash"] + mtm
        state["equity_curve"].append({"date": str(d.date()), "equity": round(equity, 2)})
        state["last_date"] = str(d.date())

    state["positions"] = positions
    return state


def equity_dataframe(state: dict) -> pd.DataFrame:
    """把 state 的權益曲線轉成 compute_risk_metrics 可吃的 DataFrame（欄位 Equity）。"""
    ec = state.get("equity_curve", [])
    if not ec:
        return pd.DataFrame(columns=["Equity"])
    df = pd.DataFrame(ec)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").rename(columns={"equity": "Equity"})
    return df


def trades_dataframe(state: dict) -> pd.DataFrame:
    return pd.DataFrame(state.get("trades", []))
