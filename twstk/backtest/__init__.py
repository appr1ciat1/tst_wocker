"""
twstk.backtest —【套件 2】歷史回測（吃 data + 可抽換策略）

    from twstk.backtest import run_backtest, RunConfig, BacktestResult

CLI：python -m twstk.backtest.runner --strategy momentum_v85 --days 1200
"""

from twstk.backtest.engine import run_backtest, RunConfig, BacktestResult
from twstk.backtest.metrics import compute_risk_metrics

__all__ = ["run_backtest", "RunConfig", "BacktestResult", "compute_risk_metrics"]
