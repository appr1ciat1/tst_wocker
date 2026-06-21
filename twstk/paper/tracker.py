"""
twstk.paper.tracker —【套件 3】每日實際資料模擬交易（4/22 起，可抽換策略）

與套件 2（回測）共用策略介面，並依策略型態自動分派：
  - WeightStrategy → 共用權重成交核心（twstk.portfolio），支援增量續跑。
  - EngineStrategy → 策略自帶事件引擎（忠實 v8.5 / SR v2 / v9）；
        事件引擎非增量，故每次都從 start 完整重跑到 end（即 replay 語意）。

用法
----
    python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy momentum_v85
    python -m twstk.paper.tracker --strategy hybrid_tiered_v9 --replay-from 2026-04-22
    python -m twstk.paper.tracker --strategy ew_momentum --top-k 5
    python -m twstk.paper.tracker            # 從上次狀態續跑到今天
    python -m twstk.paper.tracker --reset
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date

import pandas as pd

from strategies.registry import get_strategy
from strategies.base import MarketData, ExecConfig, EngineStrategy, WeightStrategy
from twstk.data import (
    fetch_prices, liquid_universe, fetch_benchmark,
    fetch_us_signals, align_us_to_tw, build_inst_flow_df, fetch_sbl_balances,
)
from twstk.portfolio import (
    PortfolioConfig, simulate_weights, new_state,
)

DEFAULT_START = "2026-04-22"           # ★每日模擬起點
STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")
try:
    from ai_report import EXTENDED_TICKERS as DEFAULT_TICKERS
except Exception:  # noqa: BLE001 — 後備靜態小池
    DEFAULT_TICKERS = [
        "2330", "2317", "2454", "2308", "2382", "2412", "2881", "2882",
        "2891", "3008", "2303", "1301", "1303", "2002",
    ]


@dataclass
class SimConfig:
    tickers: list
    capital: float = 200_000
    buy_cost: float = 0.001425
    sell_cost: float = 0.004425
    slippage: float = 0.0
    max_weight: float = 1.0
    universe_size: int = 60


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def _build_market_data(cfg, strategy, fetch_start, end_date):
    panel = fetch_prices(cfg.tickers, start_date=fetch_start, end_date=end_date)
    universe_mask = None
    if cfg.universe_size and cfg.universe_size > 0:
        universe_mask = liquid_universe(panel.close, panel.volume, top_n=cfg.universe_size)
    market_close = None
    try:
        market_close = fetch_benchmark("0050", start_date=fetch_start, end_date=end_date)
    except Exception:  # noqa: BLE001
        pass

    requires = getattr(strategy, "requires", frozenset())
    us_signals = None
    if "us_signals" in requires:
        try:
            raw = fetch_us_signals(
                start_date=panel.close.index[0].strftime("%Y-%m-%d"),
                end_date=panel.close.index[-1].strftime("%Y-%m-%d"),
            )
            us_signals = align_us_to_tw(raw, panel.close.index)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 美股訊號抓取失敗: {e}")
    inst_flow_df = inst_ratio_df = None
    if "inst_flow" in requires:
        try:
            inst_flow_df, inst_ratio_df = build_inst_flow_df(
                list(panel.close.columns), panel.close, verbose=False)
        except Exception:  # noqa: BLE001
            pass

    short_sale_df = None
    if "short_sale" in requires:
        try:
            sd = panel.close.index[0].strftime("%Y-%m-%d")
            ed = panel.close.index[-1].strftime("%Y-%m-%d")
            sbl = fetch_sbl_balances(list(panel.close.columns), start_date=sd, end_date=ed,
                                     verbose=False)
            short_sale_df = sbl.reindex(index=panel.close.index, columns=panel.close.columns)
        except Exception:  # noqa: BLE001
            pass

    return MarketData(
        close=panel.close, open=panel.open, high=panel.high,
        low=panel.low, volume=panel.volume,
        market_close=market_close, universe_mask=universe_mask,
        us_signals=us_signals, inst_flow_df=inst_flow_df, inst_ratio_df=inst_ratio_df,
        short_sale_df=short_sale_df,
    )


def run(strategy_name, start_date, end_date, cfg: SimConfig, state=None, **strat_params):
    """從 start_date 模擬到 end_date，回傳更新後的 state。"""
    strategy = get_strategy(strategy_name, **strat_params)
    fetch_start = (pd.Timestamp(start_date) - pd.Timedelta(days=220)).strftime("%Y-%m-%d")
    data = _build_market_data(cfg, strategy, fetch_start, end_date)

    if isinstance(strategy, EngineStrategy):
        # 事件引擎：完整重跑（replay 語意），重建 equity/trades
        exec_cfg = ExecConfig(
            initial_capital=cfg.capital,
            buy_cost=cfg.buy_cost, sell_cost=cfg.sell_cost, slippage=cfg.slippage,
            top_k=int(strat_params.get("top_k", 7)),
            threshold=float(strat_params.get("threshold", 2.0)),
        )
        trades_df, equity_df = strategy.run_engine(data, exec_cfg)
        lo, hi = pd.Timestamp(start_date), pd.Timestamp(end_date)
        eq = equity_df[(equity_df.index >= lo) & (equity_df.index <= hi)]
        state = new_state(start_date, cfg.capital)
        state["strategy"] = strategy_name
        state["equity_curve"] = [
            {"date": str(d.date()), "equity": round(float(v), 2)}
            for d, v in eq["Equity"].items()
        ]
        if len(eq):
            state["last_date"] = str(eq.index[-1].date())
        state["trades"] = trades_df.to_dict("records") if trades_df is not None else []
        state["positions"] = {}   # 由引擎內部管理
        return state

    if isinstance(strategy, WeightStrategy):
        weights = strategy.target_weights(data)
        pcfg = PortfolioConfig(
            initial_capital=cfg.capital,
            buy_cost=cfg.buy_cost, sell_cost=cfg.sell_cost, slippage=cfg.slippage,
            max_weight=cfg.max_weight,
        )
        if state is None:
            state = new_state(start_date, cfg.capital)
            state["strategy"] = strategy_name
        state = simulate_weights(
            weights, data.open, data.close, pcfg,
            state=state, start=start_date, end=end_date,
        )
        state["strategy"] = strategy_name
        return state

    raise TypeError(f"未知策略型態：{type(strategy)}")


def _print_summary(state):
    ec = state.get("equity_curve", [])
    if not ec:
        print("（無權益資料；可能起始日尚無交易日）")
        return
    init = state["initial_capital"]
    last = ec[-1]["equity"]
    ret = (last / init - 1) * 100
    peak, mdd = init, 0.0
    for p in ec:
        peak = max(peak, p["equity"])
        mdd = min(mdd, p["equity"] / peak - 1)
    print("\n" + "=" * 52)
    print(f"📒 Paper Trading — 策略 {state.get('strategy')}")
    print("=" * 52)
    print(f"  期間       : {state['start_date']} → {state['last_date']}")
    print(f"  初始資金   : {init:,.0f}")
    print(f"  目前權益   : {last:,.0f}  ({ret:+.2f}%)")
    print(f"  最大回撤   : {mdd * 100:.2f}%")
    print(f"  成交筆數   : {len(state.get('trades', []))}")
    print(f"  持倉中     : {len(state.get('positions', {}))} 檔")
    print("=" * 52)


def main(argv=None):
    parser = argparse.ArgumentParser(description="twstk 每日模擬交易 (4/22 起)")
    parser.add_argument("--strategy", default="momentum_v85", help="策略名稱")
    parser.add_argument("--replay-from", dest="replay_from", metavar="YYYY-MM-DD",
                        help="從指定日整段重播（清空狀態重建）")
    parser.add_argument("--replay-to", dest="replay_to", metavar="YYYY-MM-DD",
                        help="重播結束日（預設今天）")
    parser.add_argument("--reset", action="store_true", help="清空狀態後結束")
    parser.add_argument("--tickers", nargs="*", help="自訂股票池")
    parser.add_argument("--capital", type=float, default=200_000, help="初始資金")
    parser.add_argument("--universe-size", type=int, default=60, help="動態池大小 (0=停用)")
    parser.add_argument("--slippage", type=float, default=0.0, help="滑價 (0.001=10bps)")
    parser.add_argument("--top-k", type=int, help="每日最多持有檔數")
    parser.add_argument("--threshold", type=float, help="評分型：進場分數下限")
    parser.add_argument("--lookback", type=int, help="部分策略：回看天數")
    args = parser.parse_args(argv)

    if args.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print("✅ 已清空 paper 狀態")
        return 0

    cfg = SimConfig(
        tickers=args.tickers or DEFAULT_TICKERS,
        capital=args.capital, slippage=args.slippage,
        universe_size=args.universe_size,
    )
    strat_params = {}
    for key in ("top_k", "threshold", "lookback"):
        val = getattr(args, key)
        if val is not None:
            strat_params[key] = val

    end_date = args.replay_to or date.today().isoformat()

    if args.replay_from:
        print(f"🔁 重播 {args.replay_from} → {end_date}（策略 {args.strategy}）")
        state = run(args.strategy, args.replay_from, end_date, cfg, state=None, **strat_params)
    else:
        state = load_state()
        if state is None:
            print(f"（無既有狀態，從 {DEFAULT_START} 起新建）")
            state = run(args.strategy, DEFAULT_START, end_date, cfg, state=None, **strat_params)
        else:
            start = state.get("start_date", DEFAULT_START)
            print(f"➡️ 續跑至 {end_date}（策略 {args.strategy}）")
            state = run(args.strategy, start, end_date, cfg, state=state, **strat_params)

    save_state(state)
    _print_summary(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
