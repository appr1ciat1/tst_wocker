# TW Stocker v9.0 — AI 量化交易系統（雙策略架構）

中期動量 + 板塊輪動的雙策略系統。v8.5 個股動量穩健底倉 + Sector Rotation v2 板塊資金流追蹤。
美股前提（SPY/VIX/SOX）→ 板塊資金流選擇 → 板塊內選股。

> 最新重算：2026-05-26。以下數字已套用日期對齊、eval window 裁切、raw OHLCV tradability mask、`.TW/.TWO` fallback、Arithmetic Sharpe 修正。舊版 README 的高 Sharpe / crisis headline 不應再沿用。

**v9 Hybrid Tiered Risk Budgeting（新）**：在維持 v8.5 Momentum + Sector Rotation v2 alpha 的前提下，導入 Portfolio Volatility Targeting（目標年化 8-12%） + Core-Satellite 分層風險預算。Core（3-5 檔結構龍頭，如 2330）給予較高基礎曝險與較緩 scale；Satellite 嚴格受 vol 目標約束。所有 scale 決策與 Core 選取寫入 experiment registry。實盤/ paper 層以 overlay 方式運作，與既有 regime filter 相容。使用 `python paper_trade.py tiered` 及 `paper_tracker.py` 雙 book 追蹤查看即時 scale 建議。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html
📈 **Paper Trading**：https://voidful.github.io/tw_stocker/paper_trading.html

---

## 雙策略架構總覽

| | v8.5 Momentum | Sector Rotation v2 (NEW) |
|---|:---:|:---:|
| **邏輯** | 個股 cross-sectional ranking | 先選板塊 → 板塊內排名 |
| **Regime** | 台股 0050 vs MA60 | 🌍 **美股 SPY + VIX + SOX** |
| **選股因子** | Mom(20d)×3 + Trend(60MA)×1 | 板塊 flow(10/15/20d) + 板塊內動量 |
| **角色** | 穩健底倉（低 MDD） | 積極追蹤（高報酬） |
| **1200d 年化** | **+76.8%** | +53.8% |
| **1200d Sharpe** | **2.37** | 1.61 |
| **1200d MDD** | **-16.4%** | -38.0% |
| **7y 年化參考** | +39.0% | +36.6% |
| **7y Sharpe 參考** | 1.56 | 1.32 |

---

## Sector Rotation v2 — 板塊輪動策略

### 三層架構

```
Layer 1: 美股 Macro Regime（前提門檻）
  SPY trend + VIX level → 整體曝險 (0.0 ~ 1.0)
  SPY + SOX 雙空 → 幾乎停止 (0.1)
  VIX > 28 → 完全停止 (0.0)

Layer 2: 板塊資金流（主體選擇）
  7 大板塊的 10/15/20d 平均報酬加權排名
  取前 3 板塊，板塊均報酬 < -3% → 不進場

Layer 3: 板塊內選股
  momentum(20d) × 2 + trend(close > MA60) × 1
  每板塊 Top-3，合計 6~9 檔

出場: ATR TP/SL 4.0/3.0 + 20 天持倉上限
成本: 買 0.143% + 賣 0.443% + 滑價 10bps
```

### 關鍵設計：美股前提

| 條件 | 曝險 | 說明 |
|------|:---:|------|
| SPY↑ + VIX < 22 | **100%** | 全面多頭 |
| SPY↑ + VIX 22~25 | 70% | 輕微恐慌 |
| SPY↓ + VIX < 25 | 40% | 溫和空頭 |
| SPY↓ + VIX 25~28 + SPY > MA20 | 50% | 復甦允許 |
| SPY↓ + VIX 25~28 | 20% | 中等恐慌 |
| SPY + SOX 雙空 | **10%** | 最危險 |
| VIX > 28 | **0%** | 完全停止 |

### SOX 科技門檻 (v1.1: 影響所有板塊)

| 條件 | 效果 |
|------|------|
| SOX > MA60 | 全面開放 |
| SOX < MA60, mom > -3% | **所有板塊半倉**（不只科技） |
| SOX < MA60, mom < -3% | 科技禁止 + 其他半倉 |

---

## 11 段歷史危機壓測（2026-05-26 重算）

| 期間 | SR v2 Sharpe | v8.5 Sharpe | 0050 | SR MDD | VIX 均 |
|------|:---:|:---:|:---:|:---:|:---:|
| 💥 金融海嘯 '08-'09 | -1.63 | -0.86 | 0.95 | -38% | 35 |
| 💥 海嘯復甦 '09-'10 | 0.38 | 1.18 | 2.16 | -15% | 28 |
| 🦠 疫情前 '19Q4 | 1.88 | 0.70 | 1.50 | -7% | 14 |
| 🦠 疫情爆發 '20H1 | 0.63 | 1.04 | -0.34 | -15% | 35 |
| 🦠 疫後牛市 '20-'21 | 2.04 | 2.19 | 2.67 | -26% | 24 |
| ⚔️ 烏俄戰爭 '22H1 | -2.30 | -1.31 | -2.21 | -18% | 27 |
| 📉 升息衝擊 '22 | -2.18 | -1.41 | -2.02 | -26% | 26 |
| 🤖 AI 行情 '23-'24 | 1.99 | 2.48 | 2.55 | -14% | 16 |
| 🏛️ 關稅前一月 '26 | -2.01 | -0.32 | -3.50 | -11% | 26 |
| 🏛️ 關稅衝擊 '26 | 2.31 | 1.42 | 2.12 | -12% | 23 |
| 📊 近期 '26 | 2.93 | 2.38 | 2.73 | -12% | 21 |

### 00981A 對標（共存期，年化口徑）

| 期間 | SR v2 | 00981A | 差距 |
|------|:---:|:---:|:---:|
| 🏛️ 關稅前一月 | -72.6% | -1.0% | 🔴 -71.5% |
| 🏛️ 關稅衝擊 | +139.0% | +21.4% | ✅ +117.6% |
| 📊 近期 | +264.5% | +35.0% | ✅ +229.5% |

### 危機測試解讀

修正 eval window 後，SR v2 在 2022 升息、烏俄戰爭、2026 關稅前一月仍有明顯弱段；它不是單向優於 v8.5 或 0050。SR v2 的優勢主要出現在半導體/電子強趨勢與復甦段，弱點則是全球風險升溫但尚未觸發完全停手時容易被 whipsaw。

00981A 僅在 2025-05 之後有可比資料；早期 crisis 不做 00981A 比較。

---

## v8.5 Momentum 策略（保留）

### Research Gate 決策（2026-05-26）

完整 sweep 後挑出 4 組 finalist：`baseline`、`gap=1.0`、`hold=20+k=4`、`tp=3.5/sl=3.0`，再跑 2021-2025 anchored nested walk-forward。

| Candidate | Avg OOS Sharpe | Min OOS Sharpe | Avg MDD | Worst MDD | Train-selected folds |
|------|:---:|:---:|:---:|:---:|:---:|
| `gap=1.0` | **1.082** | -1.419 | -14.6% | -21.1% | 0/5 |
| `baseline` | 1.078 | -1.159 | -14.8% | -19.8% | 1/5 |
| `hold=20+k=4` | 1.034 | **-0.888** | **-14.3%** | -19.8% | **4/5** |
| `tp=3.5/sl=3.0` | 1.033 | -1.375 | -16.6% | -25.9% | 0/5 |

Nested train-selected portfolio: average OOS Sharpe 1.025，min Sharpe -0.888，max fold PBO 0.23。Candidate-set PBO = 0.94，因此本輪 **不升級新參數到 production**。Production 維持 v8.5 baseline：TP/SL ATR 4.0/3.0、Hold 20D、Top-7、Gap 1.5。

### 績效總覽（1200d：2023-02-13 → 2026-05-26）

| 指標 | 值 | 說明 |
|------|:---:|------|
| **Sharpe** | **2.365** | Arithmetic daily-return Sharpe |
| **Geom. Sharpe** | **3.010** | 年化總報酬 / 年化波動 |
| **年化報酬** | **+76.8%** | 包含交易成本 + 10bps 滑價 |
| **MDD** | **-16.4%** | 使用 raw tradable OHLCV |
| **Calmar** | **4.644** | 年化報酬/MDD |
| **Profit Factor** | **1.95** | 575 筆交易，勝率 57.7% |

### 驗證快照（2026-05-26）

| 測試 | 結果 |
|------|------|
| **v8.5 full period 2019-2026** | 年化 +39.0%，Sharpe 1.562，MDD -35.4%，1348 筆交易 |
| **v8.5 walk-forward OOS** | 4/4 正 Sharpe，3/4 Sharpe ≥ 1.0；平均 1.688，最低 0.537 |
| **OOS decay** | 平均 OOS Sharpe / full-period Sharpe = 1.08 |
| **SR v2 full period 2019-2026** | 年化 +36.6%，Sharpe 1.317，MDD -35.4%；總報酬 +748.7% vs 0050 +593.2% |
| **Monte Carlo v3** | equity_20260526，2000 runs，block=20，seed=42；5% 總報酬 +129.5%，5% MDD -24.8%，中位 Sharpe 2.21 |

### 策略公式

```
每日訊號:
  1. Universe = 過去 20 日平均成交額 Top-60
  2. 綜合評分 = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
  3. 進場: score ≥ 2.0 AND close > 60MA AND 大盤 regime ≥ 40%
  4. 跳空 > 1.5×ATR 的進場日跳過
  5. Top-7 選股（相關性 > 0.8 的替換為不相關候選）

出場 (gap-aware): ATR TP 4.0 / SL 3.0 + 20 天持倉
成本: 買 0.1425% + 賣 0.4425% + 滑價 10bps
```

---

## 快速開始

```bash
pip install -r requirements.txt

# ── v8.5 Momentum ──
python ai_report.py --show-inst

# ── Sector Rotation v2 ──
python sector_rotation_report.py                          # 預設 1200 天
python sector_rotation_report.py --start-date 2019-01-01  # 7 年回測
python sector_rotation_report.py --compare                # vs 0050

# ── 深度危機壓測 (11 段) ──
python deep_crisis_test.py

# ── 驗證工具 ──
python walk_forward.py                                    # OOS 穩定性
python walk_forward_nested.py --quick                     # Nested train→select→test gate
python monte_carlo.py --equity artifacts/equity_YYYYMMDD.csv --runs 2000 --block-size 20
python crisis_test.py                                     # 基礎危機壓測

# ── 研究審計 / 多重測試修正 ──
python sweep.py --quick                                   # 預設寫入 artifacts/experiments.sqlite
python factor_grid_search.py --mode ablation              # 寫入同一個 experiment registry
python -m validation.deflated_sharpe --equity artifacts/equity_YYYYMMDD.csv --trials 20
python -m research.experiment_registry --latest 20

# ── Paper Trading ──
python paper_trade.py signals --enrich
python paper_trade.py hardstop
python paper_trade.py tiered          # v9: 計算當前組合 vol target + tiered core/sat scale
python paper_trade.py core            # v9: 顯示 Core Holdings 建議篩選
# paper_tracker.py 每日自動維護 core/sat 雙 book equity 與 last_tiered
```

## 研究平台化工具

新增的研究層把「跑過哪些參數」變成可審計紀錄，而不是只留下漂亮表格。

| 模組 | 功能 |
|------|------|
| `research/experiment_registry.py` | SQLite registry，預設 `artifacts/experiments.sqlite`；記錄 git commit、資料快照、假說、參數空間、trial metrics、daily returns、decision |
| `validation/deflated_sharpe.py` | Deflated Sharpe Ratio，修正多重測試與非正態報酬造成的 Sharpe 膨脹 |
| `validation/pbo_cscv.py` | CSCV Probability of Backtest Overfitting，估計「train 內選到的最佳參數在 OOS 掉到下半部」的比例 |
| `walk_forward_nested.py` | Anchored train → inner parameter selection → next-year test；所有候選參數與外層 OOS 結果都寫入 registry |

`sweep.py`、`factor_grid_search.py`、`ablation_study.py` 會預設寫入同一個 registry；若只想臨時跑表格，可加 `--no-registry`。

## 專案結構

```
tw_stocker/
├── ai_report.py                  # v8.5 主程式 + CLI + HTML 報表
├── sector_rotation_report.py     # 🆕 板塊輪動 v2 回測 + 報告
├── deep_crisis_test.py           # 🆕 11 段歷史危機壓測 + 00981A
├── crisis_test.py                # 基礎危機壓力測試
├── walk_forward.py               # Anchored OOS 穩定性驗證 (v2)
├── walk_forward_nested.py        # Nested train→select→test research gate
├── monte_carlo.py                # Equity-Curve Block Bootstrap (v3)
├── sweep.py                      # 季度參數校準 + Telegram 警報
├── paper_trade.py                # Paper Trading v8 + 月報 + v9 tiered CLI
├── paper_tracker.py              # 每日自動 paper equity 追蹤（雙 book 支援）
├── research/
│   └── experiment_registry.py     # SQLite experiment audit log
├── validation/
│   ├── deflated_sharpe.py         # Deflated Sharpe Ratio
│   └── pbo_cscv.py                # CSCV Probability of Backtest Overfitting
├── strategy/
│   ├── ai_strategy.py            # 因子工程 (Mom×3 + Trend×1)
│   ├── event_backtest.py         # v8.5 事件驅動回測引擎
│   ├── us_market.py              # 🆕 美股信號 (SPY/VIX/SOX)
│   ├── sector_rotation_backtest.py # 🆕 板塊輪動回測引擎
│   ├── sector_flow.py            # 板塊資金流分析
│   ├── institutional_flow.py     # 三大法人籌碼因子
│   ├── news_sentiment.py         # 新聞情緒因子
│   ├── risk_metrics.py           # 風險指標計算 + v9 tiered/vol helpers
│   ├── core_holdings.py          # 🆕 v9 Core 多因子篩選（3-5 檔高信心）
│   ├── portfolio_vol_target.py   # 🆕 v9 Portfolio Vol Targeting + Tiered Core/Sat
│   └── benchmark.py              # Benchmark (0050 / EW)
├── artifacts/                    # 每日 CSV + 月報 + experiments.sqlite
├── .github/workflows/
│   └── update_ai_report.yml      # 每日自動執行
└── stock_report.html             # 完整交易報表
```

## 壓力測試方法論

> **資料與評估完整性**：回測可以用 `fetch_start` 提前抓資料暖機，但績效統計只從
> `eval_start` 開始；benchmark / regime filter 會使用相同的 start/end 對齊。
> OHLCV 交易價格不再全欄位 forward-fill，交易只能在 raw open/high/low/close/volume
> 完整且 volume > 0 的日期發生。
>
> ⚠️ **Monte Carlo** (`monte_carlo.py`) 對每日組合報酬率做 equity-curve block bootstrap，
> 保留了多檔同持、regime 縮放、gap sizing 等組合效應。但 bootstrap 仍假設日報酬的
> 時序結構可以被隨機重排——在極端 regime 轉換時這不成立。結果應視為
> **分布估計的參考**，不能直接當作實盤安全邊際。
>
> **OOS 穩定性** (`walk_forward.py`) 是固定參數的分段 OOS 測試；**研究閘門**
> (`walk_forward_nested.py`) 則是 nested train→select→test，用來評估參數搜尋流程本身是否過擬合。
>
> **歷史危機壓測** (`deep_crisis_test.py`) 在 11 段歷史危機做完整回測，
> 含金融海嘯、COVID、烏俄戰爭、升息、關稅衝擊，同時比較 v8.5 / SR v2 / 0050 / 00981A。

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
