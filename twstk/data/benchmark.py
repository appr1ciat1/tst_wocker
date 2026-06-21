"""
twstk.data.benchmark — Benchmark / 對標資料（0050、等權等）

純資料層 facade，沿用 strategy.benchmark 的已驗證實作。
"""

from strategy.benchmark import (
    fetch_benchmark,
    equal_weight_benchmark,
    compute_excess_return,
)

__all__ = ["fetch_benchmark", "equal_weight_benchmark", "compute_excess_return"]
