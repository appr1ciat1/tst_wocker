# TW Stocker v9 — 多策略量化系統（twstk 模組化架構）

台股 AI 量化交易系統。核心三層：**v8.5 個股動能**（穩健底倉）+ **Sector Rotation v2 板塊輪動** + **v9 Hybrid Tiered**（Portfolio Vol Targeting + Core-Satellite 分層風險預算）overlay。
2026-06 重構為 **`twstk` 三套件模組化架構**，策略與基礎設施解耦、可插拔。

> 本 fork（`appr1ciat1/tw_stocker`）**不含任何「餐費 / Meal Money」策略** —— 那是上游
> `voidful/tw_stocker` 版本的內容，與本專案無關。

📊 **線上報表（每交易日收盤後自動更新並部署 GitHub Pages）**
- [stock_report.html](https://appr1ciat1.github.io/tw_stocker/stock_report.html) — v9 主報表
- [paper_trading.html](https://appr1ciat1.github.io/tw_stocker/paper_trading.html) — Paper Trading
- [strategy_compare.html](https://appr1ciat1.github.io/tw_stocker/strategy_compare.html) — **三策略比較（v8.5 / v9 V3 / v9+反轉）回測 + Paper 時間軸折線圖**

---

## twstk 模組化架構（2026-06 重構）

把原本糾纏在 `ai_report.py` / `paper_tracker.py` 的「抓資料 / 回測 / 每日模擬 / 策略」
拆成**三個互相獨立、與策略解耦**的套件 + 一個**策略插件夾**：

```
twstk/
├── data/        【套件1 歷史數據】純資料：行情(yfinance)/universe/benchmark/
│                 美股 regime/三大法人(新版)/借券賣出(SBL)
├── portfolio.py 【目標權重成交核心】回測與每日模擬共用
├── backtest/    【套件2 歷史回測】依策略型態自動分派
├── paper/       【套件3 每日模擬交易(4/22 起)】
└── report/      compare.py → 三策略比較儀表板

strategies/      【策略插件夾】WeightStrategy / SignalStrategy / EngineStrategy
                 三種介面；新增策略不動三層基礎設施
```

詳細設計與「如何新增策略」見 [ARCHITECTURE.md](ARCHITECTURE.md)。

### 策略清單

| 名稱 | 說明 | 狀態 |
|---|---|---|
| `momentum_v85` | v8.5 個股橫向動能（Mom×3 + Trend×1，忠實事件引擎） | Production baseline |
| `sector_rotation_v2` | 板塊輪動（美股 regime + 板塊資金流 + 板塊內動量） | Research sleeve |
| `hybrid_tiered_v9` | v9：Core-Satellite + 波動目標 overlay | **Production（最佳風險調整）** |
| `reversal_20d` | 均值回歸 sleeve（與動能低相關 ~0.33，作分散用） | 分散 sleeve |
| `momentum_v9_sbl` | v9 + 借券賣出(SBL)空方 tilt | ⚠️ 實驗性（全週期未過驗證） |
| `ew_momentum` | 等權動能範例（給未來研究） | 範例 |

```bash
python -m twstk.backtest.runner --list                          # 列出策略
python -m twstk.backtest.runner --strategy hybrid_tiered_v9 --days 1200
python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy hybrid_tiered_v9
```

---

## 績效與研究結論（誠實口徑）

> ⚠️ 所有數字為**點時間快照**，會隨滾動視窗與新行情漂移；以最新一次回測 / walk-forward OOS 為準。

**全週期（2019–2026）v9 V3 在報酬與風險都優於 v8.5：**

| 策略 | 年化 | Sharpe | MDD | Calmar |
|---|---|---|---|---|
| v8.5 | ~+43% | ~1.55 | ~−44% | ~1.0 |
| **v9 V3** | **~+46%** | **~1.77** | **~−31%** | **~1.5** |

- **近期純多頭** v8.5 絕對報酬較高、但回撤更大；多頭中「同時更高報酬 + 更低風險」不可得（風險報酬鐵律）。v9 V3 在全週期與近期皆為**最佳 Calmar**。
- **因子研究（已驗證）**：融資、借券賣出(SBL)因子在近 1 年看似有效，但 **2019–2026 分年 IC 會變號、全週期 Calmar 惡化（過擬合）→ 不採為穩定 alpha**。`momentum_v9_sbl` 因此標為實驗性。
- **分散研究**：v8.5 / SR v2 / v9 相關性 0.73–0.84（同為做多動能家族）→ 混合無益；**均值回歸反轉對 v9 相關性僅 0.33（真分散）**，`v9 80% + reversal 20%` 全週期把 MDD 由 −31% 降到 −25%、Calmar 提升（換來報酬略降，近期多頭會拖累）。

**V3 生產參數**：`rotation_trigger=22%` / `crisis=30%` / `stress_sat_floor=0.85` /
`stress_core_ceiling=1.50` / 冷卻 Sat `2.1× × 16 日` / `core_alpha_trim=85%` /
不主動賣 Sat（`sat_alpha_trim=0`）。已驗證調這些參數無法在全週期超越現值。

---

## 每日比較儀表板

`twstk/report/compare.py` 產出 `strategy_compare.html`：**v8.5 / v9 V3 / v9+反轉混合**
的「歷史回測」與「Paper（自 4/22）」權益**時間軸折線圖** + 績效表，每交易日由
workflow 自動重生並部署到 Pages。

```bash
python -m twstk.report.compare                       # 預設 2022 起
python -m twstk.report.compare --start 2019-01-01    # 全週期
```

---

## Sector Rotation v2 — 板塊輪動（三層架構）

```
Layer 1: 美股 Macro Regime（前提門檻）SPY trend + VIX → 曝險 0~1；SPY+SOX 雙空→0.1；VIX>28→0
Layer 2: 板塊資金流  7 大板塊 10/15/20d 加權排名，取前 3；板塊均報酬 < -3% → 不進場
Layer 3: 板塊內選股  momentum(20d)×2 + trend(close>MA60)×1，每板塊 Top-3
出場: ATR TP/SL 4.0/3.0 + 20 天持倉上限　成本: 買 0.143% + 賣 0.443% + 滑價 10bps
```

| 美股條件 | 曝險 | | SOX 門檻 | 效果 |
|---|---|---|---|---|
| SPY↑ + VIX<22 | 100% | | SOX>MA60 | 全面開放 |
| SPY↑ + VIX 22~25 | 70% | | SOX<MA60, mom>−3% | 所有板塊半倉 |
| SPY↓ + VIX<25 | 40% | | SOX<MA60, mom<−3% | 科技禁止 + 其他半倉 |
| SPY+SOX 雙空 | 10% | | | |
| VIX>28 | 0% | | | |

---

## v8.5 Momentum — 策略公式（Production baseline）

```
每日訊號:
  1. Universe = 過去 20 日平均成交額 Top-60
  2. 綜合評分 = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
  3. 進場: score ≥ 2.0 AND close > 60MA AND 大盤 regime ≥ 40%
  4. 跳空 > 1.5×ATR 的進場日跳過；Top-7 選股(相關性 > 0.8 替換)
出場 (gap-aware): ATR TP 4.0 / SL 3.0 + 20 天持倉
成本: 買 0.1425% + 賣 0.4425% + 滑價 10bps
```

Research Gate（2026-05-26）：完整 sweep + nested walk-forward 後，Candidate-set
PBO = 0.94 → **不升級新參數**，維持 baseline（TP/SL ATR 4.0/3.0、Hold 20D、Top-7、Gap 1.5）。

---

## 快速開始

```bash
pip install -r requirements.txt

# ── 新 twstk 架構（建議）──
python -m twstk.backtest.runner --strategy hybrid_tiered_v9 --days 1200
python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy hybrid_tiered_v9
python -m twstk.report.compare --start 2022-01-01            # 產生比較儀表板

# ── 既有 CLI（仍可用）──
python ai_report.py --hybrid-tiered                          # v9 主報表
python sector_rotation_report.py --compare                  # SR v2 vs 0050
python paper_tracker.py                                      # Paper 追蹤
python deep_crisis_test.py                                   # 11 段危機壓測

# ── 驗證 / 研究 ──
python walk_forward.py
python walk_forward_nested.py --quick
python monte_carlo.py --equity artifacts/equity_YYYYMMDD.csv --runs 2000 --block-size 20
```

---

## 資料來源

| 資料 | 來源 |
|---|---|
| 台股 OHLCV / benchmark / 美股 regime | yfinance |
| 三大法人籌碼（新版） | `appr1ciat1/tw-institutional-stocker`（raw GitHub）；可用 `TW_INST_BASE_URL` 覆寫 |
| 借券賣出 SBL（法人空方） | FinMind `TaiwanDailyShortSaleBalances`（上市深歷史、上櫃約 1 年） |

---

## 每日自動更新（GitHub Actions）

`.github/workflows/update_ai_report.yml`：每交易日收盤後依序執行
`ai_report.py` → `paper_tracker.py` → `python -m twstk.report.compare`，
commit 後由 `deploy_pages.yml` 部署 `stock_report.html` / `paper_trading.html` /
`strategy_compare.html` 到 GitHub Pages。

---

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流，不構成投資建議。
歷史回測績效不代表未來；點時間數字會隨資料更新而變動。投資有風險，決策請自行負責。
