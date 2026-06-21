"""
twstk.backtest.runner —【套件 2】歷史回測 CLI

用法
----
    python -m twstk.backtest.runner --list                       # 列出可用策略
    python -m twstk.backtest.runner --strategy momentum_v85 --days 1200
    python -m twstk.backtest.runner --strategy ew_momentum --lookback 60 --top-k 5
    python -m twstk.backtest.runner --strategy momentum_v85 \
        --start-date 2019-01-01 --eval-start 2020-01-01 --inst-flow

策略以 --strategy <name> 選用（名稱由 strategies/ 插件註冊）。
策略專屬參數（如 --top-k / --threshold / --lookback）會傳給策略本身。
"""

import argparse
import sys

from strategies.registry import get_strategy, list_strategies
from twstk.backtest.engine import run_backtest, RunConfig


def _print_result(result):
    m = result.metrics
    print("\n" + "=" * 56)
    print(f"📊 回測結果 — 策略 {result.strategy_name}")
    print("=" * 56)
    print(f"  交易筆數     : {len(result.trades)}")
    print(f"  Sharpe       : {m.get('sharpe', float('nan')):.3f}")
    if m.get("geometric_sharpe") is not None:
        print(f"  Geom. Sharpe : {m.get('geometric_sharpe'):.3f}")
    ann = m.get("annual_return", m.get("ann_return", 0)) or 0
    print(f"  年化報酬     : {ann * 100:.1f}%")
    print(f"  最大回撤     : {(m.get('max_drawdown_pct', 0) or 0) * 100:.1f}%")
    if m.get("calmar") is not None:
        print(f"  Calmar       : {m.get('calmar'):.2f}")
    print("=" * 56)


def main(argv=None):
    parser = argparse.ArgumentParser(description="twstk 歷史回測 (策略可抽換)")
    parser.add_argument("--strategy", default="momentum_v85", help="策略名稱")
    parser.add_argument("--list", action="store_true", help="列出可用策略後結束")
    parser.add_argument("--tickers", nargs="*", help="自訂股票池（預設用內建池/動態池）")
    parser.add_argument("--days", type=int, default=1200, help="回測天數 (預設 1200)")
    parser.add_argument("--start-date", dest="start_date", help="起始日 YYYY-MM-DD")
    parser.add_argument("--end-date", dest="end_date", help="結束日 YYYY-MM-DD")
    parser.add_argument("--eval-start", dest="eval_start", help="績效起算日 YYYY-MM-DD")
    parser.add_argument("--universe-size", type=int, default=60, help="動態池大小 (0=停用)")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始資金")
    parser.add_argument("--slippage", type=float, default=0.0, help="滑價 (0.001=10bps)")
    parser.add_argument("--max-weight", type=float, default=1.0, help="單檔權重上限")
    parser.add_argument("--rebalance-threshold", type=float, default=0.0,
                        help="權重變動小於此值不換倉（降週轉）")
    parser.add_argument("--inst-flow", action="store_true", help="載入新版三大法人因子")
    # ── 策略專屬參數（會傳給策略）──
    parser.add_argument("--top-k", type=int, help="每日最多持有檔數")
    parser.add_argument("--threshold", type=float, help="評分型：進場分數下限")
    parser.add_argument("--lookback", type=int, help="部分策略：回看天數")
    parser.add_argument("--inst-flow-weight", type=float, help="評分型：法人因子權重")
    args = parser.parse_args(argv)

    if args.list:
        print("可用策略：")
        for name, desc in list_strategies().items():
            print(f"  - {name:<18} {desc}")
        return 0

    # 收集要傳給「策略」的參數（只傳有給的）
    strat_params = {}
    for key in ("top_k", "threshold", "lookback", "inst_flow_weight"):
        val = getattr(args, key)
        if val is not None:
            strat_params[key] = val

    cfg = RunConfig(
        tickers=args.tickers,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        eval_start=args.eval_start,
        universe_size=args.universe_size,
        initial_capital=args.capital,
        slippage=args.slippage,
        top_k=args.top_k if args.top_k is not None else 7,
        threshold=args.threshold if args.threshold is not None else 2.0,
        max_weight=args.max_weight,
        rebalance_threshold=args.rebalance_threshold,
        use_inst_flow=args.inst_flow,
    )

    strategy = get_strategy(args.strategy, **strat_params)
    result = run_backtest(strategy, cfg)
    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
