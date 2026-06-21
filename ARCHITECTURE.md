# 架構：三套件拆分 + 策略插件

把原本糾纏在 `ai_report.py` / `paper_tracker.py` 裡的「抓資料 / 回測 / 每日模擬 / 策略」
拆成**三個互相獨立、與策略解耦**的套件，外加一個**策略插件夾**。
未來要研究多種策略時，只新增插件，三層基礎設施不動。

```
twstk/
├── data/        【套件 1】歷史數據 — 純資料，零策略
│   ├── prices.py          # yfinance 台股日線 OHLCV
│   ├── universe.py        # 動態流動性池
│   ├── benchmark.py       # 0050 / 等權對標
│   ├── us_market.py       # 美股 regime (SPY/VIX/SOX)
│   ├── institutional.py   # ★新版三大法人 + 分點券商
│   └── short_sale.py      # ★借券賣出(SBL,法人空方) FinMind + 每日快取
├── portfolio.py # 🔑 目標權重「成交核心」— 回測與每日模擬共用
├── backtest/    【套件 2】歷史回測 — 吃 data + 可抽換策略
│   ├── engine.py          # run_backtest(strategy, RunConfig) → 走 portfolio 核心
│   ├── metrics.py         # Sharpe/MDD/Calmar
│   └── runner.py          # CLI
└── paper/       【套件 3】每日模擬交易（4/22 起）— 吃 data + 同一套策略
    └── tracker.py         # 走同一個 portfolio 核心 + 狀態持久化 + replay

strategies/      【策略插件夾】— 與三層完全解耦
├── base.py             # Strategy 介面：WeightStrategy / SignalStrategy / EngineStrategy
├── registry.py         # @register / get_strategy / list_strategies
├── momentum_v85.py     # ★v8.5（忠實事件引擎；同時為 SignalProducer 供 v9 取用）
├── sector_rotation_v2.py  # ★SR v2（忠實事件引擎；需 us_signals）
├── hybrid_tiered_v9.py    # ★v9 overlay（Core-Satellite + 波動目標，疊在底層 SignalProducer）
├── momentum_v9_sbl.py     # v9 + 借券賣出(SBL)空方 tilt（IC 驗證：全週期未過,謹慎用）
├── reversal.py            # ★均值回歸 sleeve（與動能低相關 0.33，作分散用）
└── ew_momentum.py      # 範例（目標權重型，給未來研究）

twstk/report/compare.py    # ★三策略比較儀表板：v8.5/v9 V3/v9+反轉混合 的回測+paper+折線圖
                           #   python -m twstk.report.compare → strategy_compare.html(每日可更新)
```

## 策略接縫（重點）：兩種執行型態

| 型態 | 基底類別 | 怎麼執行 | 用途 |
|---|---|---|---|
| **目標權重型** | `WeightStrategy`（實作 `target_weights`） | 走共用權重核心 `twstk.portfolio` | 未來研究全新策略（推薦） |
| ↳ 評分特例 | `SignalStrategy`（實作 `prepare`） | 自動轉等權 Top-K 權重 | 橫向評分型 |
| **自帶引擎型** | `EngineStrategy`（實作 `run_engine`） | 策略自己的事件引擎 | 忠實重現 v8.5 / SR v2 / v9 |

```python
class WeightStrategy(Strategy):
    def target_weights(self, data) -> pd.DataFrame: ...   # 列和 ≤ 1，0=不持有

class EngineStrategy(Strategy):
    def run_engine(self, data, exec_cfg) -> (trades_df, equity_df): ...  # 自帶引擎
```

- **權重型**共用 `twstk.portfolio.simulate_weights`（前一日決策、當日開盤換倉、扣成本、收盤 MTM），
  回測與每日模擬語意一致、可比較。
- **引擎型**（v8.5/SR v2/v9）保留原事件引擎（ATR 停利停損、跳空、tiered overlay），能重現 README 數字；
  在每日模擬端，因事件引擎非增量，採「完整重跑到今天」(replay) 語意。
- 執行層（`twstk.backtest` / `twstk.paper`）依策略型態**自動分派**，作者只需選對基底類別。

### v8.5 / SR v2 / v9 的忠實對應

| 插件 | 等同於 |
|---|---|
| `momentum_v85` | `EventDrivenBacktester(hybrid_tiered=False)` + `engineer_features` 訊號 |
| `sector_rotation_v2` | `SectorRotationBacktester` + 美股訊號對齊 + 板塊資金流 |
| `hybrid_tiered_v9` | `EventDrivenBacktester(hybrid_tiered=True, v3 生產參數)` 疊在底層 SignalProducer（預設 v8.5） |

v9 是 overlay：`get_strategy("hybrid_tiered_v9", base="momentum_v85")`。
底層需為 SignalProducer（能產生橫向評分）；SR v2 採不同引擎，無法直接套此 overlay。

## 三部分各自怎麼跑

```bash
pip install -r requirements.txt     # 需要 yfinance 等（行情用）

# ── 套件 1：抓歷史數據（純資料，可單獨用）──
python -c "from twstk.data import fetch_prices; p=fetch_prices(['2330','2317'], days=400); print(p.close.tail())"
python -c "from twstk.data import fetch_inst_rankings; print(fetch_inst_rankings(20,'up')[:3])"  # ★新版法人

# ── 套件 2：歷史回測 ──
python -m twstk.backtest.runner --list
python -m twstk.backtest.runner --strategy momentum_v85 --days 1200          # v8.5
python -m twstk.backtest.runner --strategy sector_rotation_v2 --days 1200    # SR v2
python -m twstk.backtest.runner --strategy hybrid_tiered_v9 --days 1200      # v9
python -m twstk.backtest.runner --strategy ew_momentum --lookback 60 --top-k 5
python -m twstk.backtest.runner --strategy momentum_v85 --start-date 2019-01-01 --eval-start 2020-01-01 --inst-flow

# ── 套件 3：每日模擬交易（4/22 起）──
python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy momentum_v85
python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy hybrid_tiered_v9
python -m twstk.paper.tracker                 # 從上次狀態續跑到今天
python -m twstk.paper.tracker --reset
```

## 新增一個全新策略（未來研究用）

1. 在 `strategies/` 新增 `my_strategy.py`（目標權重型，最自由）：

   ```python
   import pandas as pd
   from strategies.base import WeightStrategy, MarketData
   from strategies.registry import register

   @register("my_strategy")
   class MyStrategy(WeightStrategy):
       description = "我的新策略"
       def __init__(self, lookback=60, top_k=5, **p):
           super().__init__(lookback=lookback, top_k=top_k, **p)
           self.lookback, self.top_k = lookback, top_k

       def target_weights(self, data: MarketData) -> pd.DataFrame:
           # 隨意用 data.close / volume / inst_flow_df / us_signals ... 算出
           # (日期 × 代號) 目標權重；0 = 不持有，列和 ≤ 1
           signal = data.close.pct_change(self.lookback)
           sel = signal.rank(axis=1, ascending=False) <= self.top_k
           w = sel.astype(float)
           return w.div(w.sum(axis=1).replace(0, pd.NA), axis=0).fillna(0.0)
   ```

   若是橫向評分型，改繼承 `SignalStrategy` 實作 `prepare()` 即可（見 `momentum_v85.py`）。

2. 在 `registry._ensure_loaded()` 加一行 `from strategies import my_strategy`。

3. 直接用（回測層與模擬層完全不需修改 —— 這就是拆分的目的）：
   ```bash
   python -m twstk.backtest.runner --strategy my_strategy --lookback 60 --top-k 5
   python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy my_strategy --top-k 5
   ```

## 三大法人：新版來源

- 來源：`appr1ciat1/tw-institutional-stocker`（新版，含 baseline 校正、5/20/60/120
  視窗、分點券商 broker 系列）。
- 預設 URL（已實測可用，因該 repo 的 GitHub Pages 尚未啟用，故走 raw）：
  `https://raw.githubusercontent.com/appr1ciat1/tw-institutional-stocker/main/docs/data`
- 可用環境變數覆寫：`TW_INST_BASE_URL=...`（日後啟用 Pages 即可改成
  `https://appr1ciat1.github.io/tw-institutional-stocker/data`）。
- 舊模組 `strategy/institutional_flow.py` 已改為 shim 轉發到新版，
  因此 `ai_report.py` / `paper_trade.py` / `strategy/core_holdings.py`
  等既有呼叫端**自動使用新版資料**，無需改碼。

## 與舊程式的關係

- 新結構為**非破壞性新增**：舊的 `ai_report.py`、`sector_rotation_report.py`、
  `paper_tracker.py` 等仍可運作。
- 資料層 facade（prices/universe/benchmark/us_market）內部沿用既有、已驗證的
  `strategy/*` 純資料函式，避免重複實作造成分歧。
- 想完全切到新結構研究策略時，以 `twstk.*` + `strategies/*` 為主即可。
```
