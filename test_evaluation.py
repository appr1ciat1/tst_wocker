import pandas as pd

from strategy.evaluation import slice_evaluation_window


def test_slice_evaluation_window_rebases_equity_after_warmup():
    equity_df = pd.DataFrame(
        {"Equity": [100, 110, 121, 133.1]},
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    trades_df = pd.DataFrame(
        {
            "Entry_Date": ["2024-01-01", "2024-01-03"],
            "Return_Pct": [0.10, 0.05],
            "Days_Held": [1, 1],
            "Reason": ["Warmup", "Eval"],
        }
    )

    eval_equity, eval_trades = slice_evaluation_window(
        equity_df, trades_df, eval_start="2024-01-03", initial_capital=1_000
    )

    assert list(eval_equity.index) == list(pd.to_datetime(["2024-01-03", "2024-01-04"]))
    assert eval_equity["Equity"].round(2).tolist() == [1100.00, 1210.00]
    assert eval_trades["Reason"].tolist() == ["Eval"]


def test_slice_evaluation_window_uses_first_eval_row_without_warmup():
    equity_df = pd.DataFrame(
        {"Equity": [100, 105]},
        index=pd.to_datetime(["2024-01-03", "2024-01-04"]),
    )

    eval_equity, eval_trades = slice_evaluation_window(
        equity_df, pd.DataFrame(), eval_start="2024-01-03", initial_capital=1_000
    )

    assert eval_equity["Equity"].round(2).tolist() == [1000.00, 1050.00]
    assert eval_trades.empty
