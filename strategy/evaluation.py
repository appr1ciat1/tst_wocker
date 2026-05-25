"""Helpers for separating data warm-up windows from evaluation windows."""

import pandas as pd


def slice_evaluation_window(
    equity_df,
    trades_df=None,
    eval_start=None,
    initial_capital=1_000_000,
    trade_date_col="Entry_Date",
):
    """Return equity/trades restricted to the true evaluation window.

    Backtests may start before the evaluation period so indicators have enough
    warm-up history. This helper keeps the full backtest path for signal
    generation, but re-bases reported equity metrics from ``eval_start``.

    Trade statistics are filtered by entry date by default. Cross-boundary
    positions still affect the evaluation equity curve through mark-to-market
    PnL, but their full trade-level return is not counted as an eval-window
    trade.
    """
    if eval_start is None:
        eval_equity = pd.DataFrame() if equity_df is None else equity_df.copy()
        return eval_equity, _copy_trades(trades_df)

    if equity_df is None or equity_df.empty:
        eval_equity = pd.DataFrame() if equity_df is None else equity_df.copy()
        return eval_equity, _filter_trades(trades_df, eval_start, trade_date_col)

    eval_start_ts = pd.Timestamp(eval_start)
    equity = equity_df.copy()
    equity.index = pd.to_datetime(equity.index)
    equity = equity.sort_index()

    eval_equity = equity.loc[equity.index >= eval_start_ts].copy()
    if eval_equity.empty:
        return eval_equity, _filter_trades(trades_df, eval_start, trade_date_col)

    pre_eval = equity.loc[equity.index < eval_start_ts]
    if pre_eval.empty:
        base_equity = eval_equity["Equity"].iloc[0]
    else:
        base_equity = pre_eval["Equity"].iloc[-1]

    if pd.isna(base_equity) or base_equity <= 0:
        raise ValueError(f"Invalid base equity before eval_start={eval_start}: {base_equity}")

    eval_equity["Equity"] = initial_capital * eval_equity["Equity"] / base_equity
    eval_trades = _filter_trades(trades_df, eval_start, trade_date_col)
    return eval_equity, eval_trades


def _copy_trades(trades_df):
    if trades_df is None:
        return pd.DataFrame()
    return trades_df.copy()


def _filter_trades(trades_df, eval_start, trade_date_col):
    if trades_df is None or trades_df.empty:
        return pd.DataFrame() if trades_df is None else trades_df.copy()

    eval_trades = trades_df.copy()
    date_col = trade_date_col
    if date_col not in eval_trades.columns and "Exit_Date" in eval_trades.columns:
        date_col = "Exit_Date"
    if date_col not in eval_trades.columns:
        return eval_trades

    trade_dates = pd.to_datetime(eval_trades[date_col], errors="coerce")
    return eval_trades.loc[trade_dates >= pd.Timestamp(eval_start)].copy()
