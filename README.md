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
| **GUARD** | `mom_guard` | ~52% | **1.78** | -27% | 1.93 | ~1040 | 弱勢去風險＋相關性分散選股，不加碼，最穩健 |
| **SURGE** | `mom_surge` | ~59% | 1.87 | **-21%** | 2.73 | ~840 | 去風險 + 分段強勢加碼 |
| **SURGE PRO** | `mom_surge_pro` | **~66%** | 1.97 | -23% | **2.88** | ~780 | 更激進分段加碼，報酬最高 |

- **GUARD / SURGE / SURGE PRO 皆同時改善報酬與回撤**（非用更深回撤換報酬）：相對 v8.5，年化更高、MDD 從 -41% 大幅收斂到 -21~-25%。
- **過擬合檢驗**：多元池 PBO 0.09–0.34（< 0.5）、Deflated Sharpe ≈ 1.0、nested walk-forward（train→select→test）逐年 OOS 不低於 baseline。
- **2022 升息失效年**：v8.5 OOS Sharpe -1.33 → GUARD -1.20 / SURGE **-0.52** / SURGE PRO -1.13（SURGE 最防守；SURGE PRO 換更高報酬故 2022 較弱）。

---

## 各策略定義（單一真實來源：`strategies/optimized_v85.py`）

四策略共用同一組 v8.5 評分（Mom×3 + Trend×1，`MomentumV85.prepare()`），差別只在事件引擎的風控/sizing 參數：

- **GUARD**：`sl_atr=3.5` + graduated regime（`regime_floor=0` 最弱全出）+ breadth-aware + `dynamic_topk`；**不加碼**。
  2026-07 起加入 **corr_select 相關性分散選股**（效率前緣/共變異數觀點）：greedy 依評分選股，候選與
  (持倉∪已選) 的 60 日相關 >0.7 之檔數達 2 即跳過（同聚落最多 2 檔）。驗證：ann 43.2→51.6%、
  Sharpe 1.54→1.78、MDD -34.2→-26.8%、2022 -14.9→-10.3%，鄰域 (0.65–0.75 / 40–80日) 全穩健。
  **同機制在 v8.5 / SURGE / SURGE PRO 測試皆變差**（加碼型策略的 alpha 靠集中聚落）故僅 GUARD 採用；
  cap=1（嚴禁同群）亦全軍覆沒——台股動量聚落太強，完全禁掉會錯過主升段。
- **SURGE**：GUARD 去風險不變 + **分段強勢加碼**（只在 0050>MA60/MA20 且 breadth 高、VIX 低時放大單筆）。
  四段式單筆部位：弱 0% / 強 12.5% / 更強(breadth≥.65,VIX≤20) 14.5% / 最強(breadth≥.75,VIX≤15) 17%。
- **SURGE PRO**：SURGE 去風險不變 + **更激進分段加碼**（VIX 門檻放寬 28、tier 倍數更高、`max_regime_scale=1.9`、`hold_days=25`）。
  四段式：弱 0% / 強 12.5% / 更強 17% / 最強 18.5%。
- **v8.5 baseline**：`momentum_v85`，binary regime、無去風險、無加碼。

引擎（向後相容）新增：`EventDrivenBacktester(strong_tiers=[(breadth,vix,mult),...])` 分段加碼、`run(..., vix_series=)` 可注入 VIX、
`corr_select_max/window/cap` 相關性分散選股（greedy，跳過與持倉∪已選高相關者，不回補——寧缺勿濫）。

---

## 各策略特色與最適市場情境

四策略「**選股訊號相同、弱勢去風險邏輯相同**」，差別只在「強勢時加碼的積極度」與「進場挑剔度」。因此各自最吃香的市場狀態不同：

| 策略 | 一句話特色 | 最有優勢的市場情境 | 較吃虧的情境 |
|---|---|---|---|
| **v8.5** | 純動量、永遠滿倉、不去風險不加碼 | 持續且廣泛的單向強多頭（滿倉吃滿漲幅，近 90 天 +72%） | 升息／系統性崩盤年（無去風險，MDD −41% 最深，2022 最慘） |
| **GUARD** | 純去風險、不加碼、最分散（＋相關性聚落上限：同群 ≤2 檔） | 廣泛溫和上漲／雨露均霑的齊漲（分散參與勝過集中）；震盪盤（弱勢自動減碼） | 少數強勢股暴衝的集中行情（不加碼，報酬輸 SURGE/PRO） |
| **SURGE** | 去風險 + 適度加碼，**回撤最淺、崩盤抗跌最好** | 「漲跌互現但長期向上」的波段；預期會有崩盤、想要抗跌的環境（2022 OOS Sharpe −0.52 最佳） | 極端強勢延續多頭（加碼較保守，報酬略輸 PRO） |
| **SURGE PRO** | 去風險 + 激進加碼，**報酬／Sharpe／Calmar 全期最高** | 強勢、低波動（VIX 低）、市場廣度高的延續多頭；少數龍頭領漲時集中加碼最吃香 | 升息／崩盤年（2022 −1.13 最痛）；廣泛齊漲時集中不如分散（短期被 GUARD 反超） |

**反查：這種行情該用哪個？**

| 市場情境 | 最適策略 | 為什麼 |
|---|---|---|
| 🚀 強勢延續多頭、VIX 低、龍頭領漲 | **SURGE PRO** | 激進分段加碼把報酬最大化 |
| 📈 漲跌互現、長期向上的波段 | **SURGE** | 去風險＋適度加碼的最佳平衡 |
| 🌊 廣泛齊漲（雨露均霑） | **GUARD / v8.5** | 分散／滿倉廣泛參與勝過集中加碼 |
| 〽️ 震盪盤整、方向不明 | **GUARD / SURGE** | graduated 去風險在弱勢自動減碼保護 |
| 💥 升息／系統性崩盤（如 2022） | **SURGE**（最防守） | 去風險＋不過度集中，崩盤年 OOS 最佳 |

> **沒有單一「最好」**：以全期 2019–2026 風險調整後報酬看 **SURGE PRO 數字最強**（年化／Sharpe／Calmar／PBO 皆居首）；但 **SURGE 的全天候平衡最佳**（回撤最淺、崩盤年防守最好，僅少約 8pp 年化）。
> **要榨乾回測優勢且能扛崩盤年 → SURGE PRO；務實怕崩盤 → SURGE。** GUARD 多被 SURGE 支配，價值在「最單純、不依賴加碼判斷」。Paper 頁預設追蹤 **SURGE PRO**（最高報酬）。

---

## 策略方法論：每一層的邏輯、方法與理論依據

策略由五層組成，每層各自解決一個問題、各有明確的理論依據。**訊號決定「買什麼」，其餘四層決定「買多少、何時買、何時出、怎麼不騙自己」。**

### 第 1 層｜訊號：橫斷面動量（買什麼）
- **做法**：v8.5 評分 = 20 日動量 ×3 + 60MA 趨勢 ×1，在流動性 Top-60 池內做**橫斷面排名**；每日取「評分 ≥2.0 且站上 60MA」的 Top-7。
- **依據**：橫斷面動量效應（Jegadeesh & Titman, 1993 — 過去 3–12 個月贏家續強）；趨勢/MA 濾網（時間序列動量，Moskowitz–Ooi–Pedersen, 2012 的概念）。
- **關鍵定位**：動量評分本身就是「預期報酬 μ 的自有觀點」。Markowitz 框架的死穴是 μ 極難估（用歷史平均會整條前緣都錯）——本系統用動量觀點取代歷史均值來回答 μ，即 **Black–Litterman「以主觀觀點修正 μ」的精神**；因此 **Σ（共變異數）只用於風險端，絕不讓它決定買什麼**（見第 4 層）。

### 第 2 層｜出場：波動標準化風控（何時出）
- **做法**：ATR 停利 4×／停損 3–3.5×（Wilder ATR，引擎內部計算）＋最長持有 20–25 日強制出場＋開盤跳空 >1.5×ATR 放棄進場＋**連續虧損 3 筆熔斷**暫停新倉（`consec_loss_limit=3`）＋權益回撤暫停。
- **依據**：以 ATR 把停損距離「波動標準化」，使高低波動股承擔一致的風險預算（固定百分比停損做不到）；熔斷是對「訊號失效期」的簡單貝氏防禦——連錯代表環境變了，先停手。

### 第 3 層｜Regime：環境決定倉位（何時買、買多大）
- **做法**：graduated regime（0050 相對 60MA/20MA 漸進調整倉位，`regime_floor=0` 最弱時全出）＋市場廣度 breadth＋VIX。SURGE/SURGE PRO 再加**分段強勢加碼** `strong_tiers`：僅在「廣度高＋VIX 低」的強勢環境把單筆從 10% 分段放大到 12.5–18.5%。
- **依據**：動量策略最大的尾部風險是「動量崩潰」（momentum crash，Daniel & Moskowitz, 2016——崩盤反彈期動量重挫），regime 濾網+去風險就是對此的直接防禦；2022 升息年的實測（v8.5 -41% MDD vs GUARD/SURGE -21~-27%）是本系統的活教材。
- **界線（測試得出）**：加碼只做「環境門檻的橫斷面放大」，**不做時間序列 Vol Target 槓桿調節**——v9（Portfolio Vol Target + Core-Satellite）實測報酬回撤皆遜於 v8.5 且 PBO 0.94，已淘汰。

### 第 4 層｜組合：相關性分散（效率前緣／共變異數觀點）
- **理論**：Markowitz (1952) 效率前緣——組合風險 σₚ=√(wᵀΣw) 取決於**資產間相關性**而非個股波動加總；同漲同跌的持股沒有分散效果。入門實作參考：[FinvestNote《用 Python 繪製效率前緣》](https://finvestnote.com/posts/efficient-frontier-and-python/)。
- **本系統的實證問題**：動量訊號天生擠同族群。實測 Top-7 訊號股平均 pairwise 相關 0.37、聚落內 0.6–0.8（如華邦電×南亞科 0.82、群創×彩晶 0.68）→ 有效獨立注數 N/(1+(N−1)ρ̄) **≈ 2.2 注**——名義上分散 7 檔、實際像押 2 檔，是 MDD 的隱形來源。
- **落地（GUARD 的 `corr_select`，2026-07）**：greedy 依評分選股，候選與（持倉∪已選）60 日相關 >0.7 的檔數達 2 即跳過（同聚落 ≤2 檔）、不回補。驗證：ann 43→52%、Sharpe 1.54→1.78、MDD -34→-27%、2022 年 -15→-10%，鄰域參數全穩健。
- **兩個由驗證劃出的界線**：(a) **不做完整 Markowitz/MVP**——風險極小化會系統性剔除高動量高波動股（該文 MVP 示範：JNJ 佔 53.77%、NVDA/META 壓到 0%），等於殺死動量 alpha；Σ 只管分散，μ 由動量觀點決定。(b) **加碼型策略（SURGE/SURGE PRO）不用 corr_select**——它們的 alpha 正是「強勢時集中壓動量聚落」，實測加上後年化 -4~-12pp；嚴禁同群（cap=1）在台股也全面變差，同聚落 ≤2 檔才是甜蜜點。

### 第 5 層｜驗證：如何避免騙自己（所有參數的守門員）
- **相對基準約束搜尋**：任何新配置必須「MDD 不深於 v8.5+3pp、逐年 OOS Sharpe 不低於基準、交易數不因過度濾網崩掉」才收。
- **過擬合檢驗**：PBO/CSCV（Bailey et al.）<0.5、Deflated Sharpe（Bailey & López de Prado）、nested walk-forward（train→select→test）。
- **穩健性紀律**：參數採用前做鄰域掃描（要的是 plateau 不是刀鋒值），且**只驗證不挑事後最佳**；新舊配置比較必須**同一次 run 內 off vs on**（yfinance 資料修訂＋路徑依賴會讓跨日數字漂移，拿舊數字對照會得出假結論）。
- **負結果也保留**：測過但不採用——完整 Markowitz/MVP、v9 Vol Target（PBO 0.94）、法人 5/10/20 日籌碼出場（`inst_hold_exit`）、低勝率風報比 gate（`low_wr_rr_gate`）、corr_select 用於加碼型策略、cap=1 嚴禁同群。待評估：Top-K 內逆波動（1/ATR）加權、SURGE PRO/SURGE 70/30 資金混搭。

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
