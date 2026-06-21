"""
twstk.backtest.engine —【套件 2】歷史回測引擎（與策略解耦）

依策略型態自動分派執行方式：
  - WeightStrategy → 共用權重成交核心（twstk.portfolio）
  - EngineStrategy → 策略自帶的事件引擎（忠實重現 v8.5 / SR v2 / v9）

資料層只抓策略真正需要的東西（看 strategy.requires，例如 us_signals）。
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from strategies.base import (
    Strategy, WeightStrategy, EngineStrategy, MarketData, ExecConfig,
)

from twstk.data import (
    fetch_prices, liquid_universe, fetch_benchmark,
    fetch_us_signals, align_us_to_tw, build_inst_flow_df, fetch_sbl_balances,
)
from twstk.portfolio import (
    PortfolioConfig, simulate_weights, equity_dataframe, trades_dataframe,
)
from twstk.backtest.metrics import compute_risk_metrics


# 預設台股池（可由 CLI 覆寫）。
# 與既有 v8.5 / SR v2 / v9 回測一致，預設使用 ai_report 的 116 檔 EXTENDED_TICKERS，
# 動態流動性池再從中取 Top-N；如此 twstk 的回測數字才能對齊 compare_v85_v9 等既有腳本。
try:
    from ai_report import EXTENDED_TICKERS as DEFAULT_TICKERS
except Exception:  # noqa: BLE001 — 後備靜態小池
    DEFAULT_TICKERS = [
        "2330", "2317", "2454", "2308", "2382", "2412", "2881", "2882",
        "2891", "3008", "2303", "1301", "1303", "2002",
    ]


@dataclass
class RunConfig:
    """回測執行設定（資料區間 + 資金/成本/部位）。"""
    tickers: Optional[list] = None
    days: int = 1200
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    eval_start: Optional[str] = None
    universe_size: int = 60
    initial_capital: float = 1_000_000
    buy_cost: float = 0.001425
    sell_cost: float = 0.004425
    slippage: float = 0.0
    top_k: int = 7
    threshold: float = 2.0
    max_weight: float = 1.0                # WeightStrategy 用
    rebalance_threshold: float = 0.0       # WeightStrategy 用
    benchmark_ticker: str = "0050"
    use_inst_flow: bool = False            # ★載入新版三大法人因子


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict
    weights: pd.DataFrame = field(default=None)
    benchmark_equity: Optional[pd.Series] = None
    strategy_name: str = ""


def build_market_data(cfg: RunConfig, strategy: Strategy) -> MarketData:
    """依設定 + 策略需求組出乾淨的 MarketData（純資料層）。"""
    tickers = cfg.tickers or DEFAULT_TICKERS
    panel = fetch_prices(
        tickers, days=cfg.days, start_date=cfg.start_date, end_date=cfg.end_date,
    )

    universe_mask = None
    if cfg.universe_size and cfg.universe_size > 0:
        universe_mask = liquid_universe(panel.close, panel.volume, top_n=cfg.universe_size)

    market_close = None
    try:
        bench = fetch_benchmark(
            cfg.benchmark_ticker,
            start_date=cfg.start_date, end_date=cfg.end_date, days=cfg.days,
        )
        market_close = bench
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ benchmark 抓取失敗，停用 regime market_close: {e}")

    requires = getattr(strategy, "requires", frozenset())

    us_signals = None
    if "us_signals" in requires:
        try:
            raw = fetch_us_signals(
                start_date=(cfg.start_date or panel.close.index[0].strftime("%Y-%m-%d")),
                end_date=(cfg.end_date or panel.close.index[-1].strftime("%Y-%m-%d")),
            )
            us_signals = align_us_to_tw(raw, panel.close.index)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 美股訊號抓取失敗: {e}")

    inst_flow_df = inst_ratio_df = None
    if cfg.use_inst_flow or "inst_flow" in requires:
        try:
            inst_flow_df, inst_ratio_df = build_inst_flow_df(
                list(panel.close.columns), panel.close, verbose=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 三大法人資料抓取失敗，跳過: {e}")

    short_sale_df = None
    if "short_sale" in requires:
        try:
            sd = panel.close.index[0].strftime("%Y-%m-%d")
            ed = panel.close.index[-1].strftime("%Y-%m-%d")
            sbl = fetch_sbl_balances(list(panel.close.columns), start_date=sd, end_date=ed)
            short_sale_df = sbl.reindex(index=panel.close.index, columns=panel.close.columns)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 借券(SBL)資料抓取失敗，跳過: {e}")

    return MarketData(
        close=panel.close, open=panel.open, high=panel.high,
        low=panel.low, volume=panel.volume,
        market_close=market_close, universe_mask=universe_mask,
        us_signals=us_signals,
        inst_flow_df=inst_flow_df, inst_ratio_df=inst_ratio_df,
        short_sale_df=short_sale_df,
    )


def run_backtest(strategy: Strategy, cfg: RunConfig) -> BacktestResult:
    """以指定策略 + 設定跑一次歷史回測（依策略型態自動分派）。"""
    print(f"🚀 回測策略：{strategy.name} — {getattr(strategy, 'description', '')}")
    data = build_market_data(cfg, strategy)

    weights = None
    if isinstance(strategy, EngineStrategy):
        # 自帶引擎（忠實 v8.5 / SR v2 / v9）
        exec_cfg = ExecConfig(
            initial_capital=cfg.initial_capital,
            buy_cost=cfg.buy_cost, sell_cost=cfg.sell_cost, slippage=cfg.slippage,
            top_k=cfg.top_k, threshold=cfg.threshold,
        )
        trades_df, equity_df = strategy.run_engine(data, exec_cfg)
    elif isinstance(strategy, WeightStrategy):
        # 共用權重成交核心
        weights = strategy.target_weights(data)
        pcfg = PortfolioConfig(
            initial_capital=cfg.initial_capital,
            buy_cost=cfg.buy_cost, sell_cost=cfg.sell_cost, slippage=cfg.slippage,
            max_weight=cfg.max_weight, rebalance_threshold=cfg.rebalance_threshold,
        )
        state = simulate_weights(
            weights, data.open, data.close, pcfg,
            start=cfg.eval_start or cfg.start_date, end=cfg.end_date,
        )
        equity_df = equity_dataframe(state)
        trades_df = trades_dataframe(state)
    else:
        raise TypeError(f"未知策略型態：{type(strategy)}（需為 WeightStrategy 或 EngineStrategy）")

    eval_equity = equity_df
    if cfg.eval_start and not equity_df.empty:
        eval_equity = equity_df[equity_df.index >= pd.Timestamp(cfg.eval_start)]
    metrics = compute_risk_metrics(eval_equity, trades_df, cfg.initial_capital)

    benchmark_equity = None
    try:
        bstart = cfg.start_date
        if bstart is None and not equity_df.empty:
            bstart = str(equity_df.index[0].date())
        benchmark_equity = fetch_benchmark(
            cfg.benchmark_ticker, start_date=bstart, end_date=cfg.end_date,
        )
    except Exception:  # noqa: BLE001
        pass

    return BacktestResult(
        trades=trades_df, equity=equity_df, metrics=metrics,
        weights=weights, benchmark_equity=benchmark_equity,
        strategy_name=strategy.name,
    )
