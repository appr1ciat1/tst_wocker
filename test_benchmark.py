import numpy as np
import pandas as pd

from strategy import benchmark


def test_fetch_benchmark_drops_invalid_close_rows(monkeypatch):
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    downloaded = pd.DataFrame(
        {"Close": [100.0, 105.0, np.nan, np.inf]},
        index=idx,
    )

    monkeypatch.setattr(benchmark.yf, "download", lambda *args, **kwargs: downloaded)

    result = benchmark.fetch_benchmark("0050", start_date="2024-01-01", end_date="2024-01-05")

    assert list(result.index) == list(idx[:2])
    assert result.tolist() == [1.0, 1.05]
    assert not result.isna().any()
