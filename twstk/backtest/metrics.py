"""
twstk.backtest.metrics — 績效指標

facade，沿用 strategy.risk_metrics 的已驗證實作（Sharpe / MDD / Calmar ...）。
"""

from strategy.risk_metrics import compute_risk_metrics

__all__ = ["compute_risk_metrics"]
