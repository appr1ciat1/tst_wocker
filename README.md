# TW Stocker — v8.5 約束優化四策略（v8.5 / GUARD / SURGE / SURGE PRO）

固定純 **v8.5 橫向動量**為唯一 baseline，在「**相對 v8.5**」的硬約束下
（MDD 不深於 baseline + 3pp、逐年 OOS Sharpe 不低於 baseline、PBO < 0.5、交易數不因過度濾網崩掉）
做 **constrained parameter search + regime-aware sizing**，產出三個正式優化策略 + 純 v8.5 基準。
每日收盤後自動全期回測、產出四份報表並部署到 GitHub Pages。

> **更新 2026-06-27**：v9 Hybrid Tiered（Core-Satellite + Vol Target）經多次驗證**報酬與回撤皆遜於 v8.5**，
> 已淘汰為背景參考（見文末）。現以 **v8.5 家族四策略**為主線。舊版 README 的 v9 headline 不再沿用。

---

## 📊 線上報表（GitHub Pages，每工作日收盤後自動更新）

**https://appr1ciat1.github.io/tw_stocker/** — 策略選單（四策略 + Paper）

| 卡片 | 內容 |
|---|---|
| [SURGE PRO](https://appr1ciat1.github.io/tw_stocker/report_surge_pro.html) | 最強策略：去風險 + 更激進分段加碼 |
| [SURGE](https://appr1ciat1.github.io/tw_stocker/report_surge.html) | 去風險 + 分段強勢加碼 |
| [GUARD](https://appr1ciat1.github.io/tw_stocker/report_guard.html) | 弱勢去風險，不加碼，最穩健 |
| [v8.5 基準](https://appr1ciat1.github.io/tw_stocker/report_v85.html) | 純動量 v8.5，優化前基準 |
| [Paper Trading](https://appr1ciat1.github.io/tw_stocker/paper_trading.html) | 模擬實盤 —— **追蹤 SURGE PRO**（最強策略）的每日訊號 |

> 報表數字為**全期 2019-01-01 → 回測當日**（`--eval-start 2019-01-01`），與下表一致；
> 點時間值，會隨資料更新而漂移，非恆定保證。

---

## 四策略（全期 2019-2026，動態 Top-60，引擎內部 ATR，`consec_loss_limit=3`）

| 策略 | 註冊名 | 年化 | Sharpe | MDD | Calmar | 交易 | 角色 |
|---|---|---:|---:|---:|---:|---:|---|
| **v8.5 baseline** | `momentum_v85` | ~40% | 1.43 | **-41%** | 0.98 | ~940 | 優化前基準（純動量 binary regime）|
| **GUARD** | `mom_guard` | ~51% | **1.78** | -25% | 2.08 | ~1010 | 弱勢去風險，不加碼，最穩健 |
| **SURGE** | `mom_surge` | ~59% | 1.87 | **-21%** | 2.73 | ~840 | 去風險 + 分段強勢加碼 |
| **SURGE PRO** | `mom_surge_pro` | **~66%** | 1.97 | -23% | **2.88** | ~780 | 更激進分段加碼，報酬最高 |

- **GUARD / SURGE / SURGE PRO 皆同時改善報酬與回撤**（非用更深回撤換報酬）：相對 v8.5，年化更高、MDD 從 -41% 大幅收斂到 -21~-25%。
- **過擬合檢驗**：多元池 PBO 0.09–0.34（< 0.5）、Deflated Sharpe ≈ 1.0、nested walk-forward（train→select→test）逐年 OOS 不低於 baseline。
- **2022 升息失效年**：v8.5 OOS Sharpe -1.33 → GUARD -1.20 / SURGE **-0.52** / SURGE PRO -1.13（SURGE 最防守；SURGE PRO 換更高報酬故 2022 較弱）。

---

## 各策略定義（單一真實來源：`strategies/optimized_v85.py`）

四策略共用同一組 v8.5 評分（Mom×3 + Trend×1，`MomentumV85.prepare()`），差別只在事件引擎的風控/sizing 參數：

- **GUARD**：`sl_atr=3.5` + graduated regime（`regime_floor=0` 最弱全出）+ breadth-aware + `dynamic_topk`；**不加碼**。
- **SURGE**：GUARD 去風險不變 + **分段強勢加碼**（只在 0050>MA60/MA20 且 breadth 高、VIX 低時放大單筆）。
  四段式單筆部位：弱 0% / 強 12.5% / 更強(breadth≥.65,VIX≤20) 14.5% / 最強(breadth≥.75,VIX≤15) 17%。
- **SURGE PRO**：SURGE 去風險不變 + **更激進分段加碼**（VIX 門檻放寬 28、tier 倍數更高、`max_regime_scale=1.9`、`hold_days=25`）。
  四段式：弱 0% / 強 12.5% / 更強 17% / 最強 18.5%。
- **v8.5 baseline**：`momentum_v85`，binary regime、無去風險、無加碼。

引擎（向後相容）新增：`EventDrivenBacktester(strong_tiers=[(breadth,vix,mult),...])` 分段加碼、`run(..., vix_series=)` 可注入 VIX。

---

## 跑法（CLI）

```bash
# 任一策略全期回測（twstk runner；不加 --eval-start 即全期）
python -m twstk.backtest.runner --strategy mom_surge_pro --start-date 2019-01-01

# 列出所有策略
python -m twstk.backtest.runner --list

# 產生 HTML 報表（ai_report.py；務必加 --eval-start 看全期、--consec-loss-limit 3）
python ai_report.py --no-hybrid-tiered --consec-loss-limit 3 \
  --sl-atr 3.5 --hold-days 25 --position-size 0.10 \
  --regime-floor 0.0 --dynamic-topk --dynamic-gap-filter \
  --regime-sizing --strong-regime-mult 1.25 --strong-breadth-min 0.55 --strong-vix-max 28.0 \
  --max-regime-scale 1.9 --strong-tiers "0.62,18,1.7;0.72,15,1.85" \
  --start-date 2019-01-01 --eval-start 2019-01-01      # ← SURGE PRO
```

完整指令對照與驗證細節見 [`artifacts/v85_optimization_result.md`](artifacts/v85_optimization_result.md)。

### ⚠️ 兩個會誤導結果的關鍵 gotcha
1. **ai_report 必加 `--consec-loss-limit 3`**：此旗標預設 99（停用連損熔斷），少了它 MDD 會從 -23% 惡化到 -40%。
   `momentum_v85` / twstk runner 走引擎預設 3，故自帶此保護。
2. **ai_report 看全期數字必加 `--eval-start 2019-01-01`**：不加時用預設視窗，會嚴重低估激進策略
   （例：SURGE PRO 會顯示偏低、MDD 偏深、順序甚至反轉）。twstk runner 不加 `--eval-start` 即全期。

---

## 每日 pipeline（GitHub Actions）

`.github/workflows/update_ai_report.yml`（每工作日 UTC 09:17 + 可手動 `workflow_dispatch`）：
依序全期重跑 **v8.5 → GUARD → SURGE → SURGE PRO**，每份報表的標題改為各自策略名，
產出 `report_v85 / report_guard / report_surge / report_surge_pro.html` + 各自圖表；
**SURGE PRO 最後跑**，使 `stock_report.html` 與 `artifacts/orders_*.json` = SURGE PRO，故
`paper_tracker.py` 追蹤的是 **SURGE PRO** 的每日訊號。`deploy_pages.yml` 接著部署到 GitHub Pages。

> workflow 需 `permissions: contents: write` 才能 commit 報表回 repo（已設）。

---

## 架構（三套件 + 策略插件）

```
twstk/            三層基礎設施
  data/           純資料層（yfinance 股價/0050、appr1ciat1 三大法人、SBL、美股訊號）
  backtest/       歷史回測 CLI（runner）+ 指標
  paper/          每日 paper 模擬
strategies/       策略插件（@register）：momentum_v85 / optimized_v85(mom_guard/surge/surge_pro)
                  / sector_rotation_v2 / hybrid_tiered_v9 / reversal / ew_momentum
strategy/         共用引擎與因子（event_backtest 事件引擎、ai_strategy 評分、risk_metrics…）
validation/       PBO(CSCV) / Deflated Sharpe
research/          constrained_search / validate_* / tune_surge_broad …（搜尋與驗證工具）
ai_report.py      事件驅動回測 + HTML 報表/交易計畫產生器
paper_tracker.py  讀當日訂單追蹤 TP/SL/時間出場，累積 paper 權益曲線
```

新策略：在 `strategies/` 下用 `@register("name")` 註冊，並於 `strategies/registry.py` 匯入即可。

---

## 背景 / 歷史（為何回到 v8.5 家族）

- **v8.5 Momentum**：個股 cross-sectional ranking（Mom 20d×3 + Trend 60MA×1），台股 0050/MA60 regime。優化前的底層 alpha。
- **Sector Rotation v2**：美股 SPY/VIX/SOX regime + 板塊資金流 + 板塊內動量。MDD 較深（~-38%），僅適合當小比例 sleeve（10–25%），不宜當主體。
- **v9 Hybrid Tiered**（已淘汰）：在 v8.5 上疊 Portfolio Vol Target + Core-Satellite。多次驗證
  （含 2026-06 行情、nested walk-forward PBO=0.94）顯示**報酬與回撤皆遜於 v8.5**，故不再作為主線。
- 測過但**未採用**：法人 5/10/20 日籌碼退場（`inst_hold_exit`）與低勝率風報比 gate（`low_wr_rr_gate`）——
  在本約束下都降低目標、無正貢獻；引擎仍保留旗標可選用。

> 績效皆為點時間回測，**非未來保證**。請以最新一次回測 / walk-forward OOS 為準。
