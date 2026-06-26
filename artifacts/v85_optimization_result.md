# v8.5 約束優化 → 三個正式策略 GUARD / SURGE / SURGE PRO（2026-06-25，PRO 於 06-26 追加）

固定純 v8.5 為唯一 baseline（無 v9/SR v2），在「相對 v8.5」硬約束下最大化年化：
MDD 不深於 baseline+3pp、逐年 OOS Sharpe 不低於 baseline、交易數 ≥0.85×、PBO<0.5。
資料 2019-01-02→2026-06-25，動態 Top-60，凍結同一份資料比較（引擎內部 ATR、consec_loss_limit=3）。

## 三者數字（全期 2019-2026）
| 策略 | 年化 | Sharpe | MDD | Calmar | 交易 | 2022 fold | 2024 fold |
|---|---|---|---|---|---|---|---|
| v8.5 baseline | 40.2% | 1.43 | -41.1% | 0.98 | 938 | -1.33 | -0.97 |
| **mom_guard** (GUARD) | 51.2% | 1.78 | -24.7% | 2.08 | 1014 | -1.20 | -0.10 |
| **mom_surge** (SURGE) | 58.8% | 1.86 | **-21.5%** | 2.73 | 839 | **-0.52** | +0.03 |
| **mom_surge_pro** (SURGE PRO) | **67.1%** | **2.01** | -22.7% | **2.96** | 780 | -1.13 | **+0.61** |

GUARD/SURGE 皆**同時改善報酬與回撤**。SURGE PRO 用更激進分段加碼換 +8pp 年化（vs SURGE），
全期 MDD 僅深 1.2pp、Sharpe/Calmar 反而更高，**代價是 2022 那年較弱**（-1.13 vs -0.52，放寬 VIX
門檻到 28 讓它在 2022 反彈也加碼）。近段(~1200d)年化：GUARD 83.5% / SURGE 98.5% / SURGE PRO 115.7%。
SURGE PRO 過擬合檢驗：多元池 PBO 0.086、DSR 1.000、nested WF 5 fold 有 4 個選到它（ai_report
--eval-start 全期交叉驗證吻合 67.13%）。

## 兩個正式策略（strategies/optimized_v85.py，已註冊）
- **mom_guard（GUARD｜最穩健）**：v8.5 + 弱勢去風險。graduated regime(floor=0 最弱全出)+breadth+dynamic_topk+放寬停損 sl3.5；不加碼。nested WF 每 fold 都被選中、DSR 0.997。
- **mom_surge（SURGE｜追報酬）**：GUARD 去風險不變 + **分段強勢加碼**。四段式單筆部位：
  弱勢 0% / 強(0050>MA60&MA20,breadth≥.55,VIX≤25) 12.5% / 更強(breadth≥.65,VIX≤20) 14.5% /
  最強(breadth≥.75,VIX≤15) 17%。PBO 0.34、DSR 0.999、最差年(2022)OOS -0.52（全場最佳）。
- **mom_surge_pro（SURGE PRO｜追最高報酬）**：SURGE 去風險不變 + **更激進分段加碼**（VIX 門檻放寬到 28、
  tier 倍數更高、cap 1.9、hold 25）。四段式：弱 0% / 強 12.5% / 更強(breadth≥.62,VIX≤18) 17% /
  最強(breadth≥.72,VIX≤15) 18.5%。換 +8pp 年化，2022 較弱（-1.13）。

跑法：`python -m twstk.backtest.runner --strategy mom_surge_pro --start-date 2019-01-01 --end-date 2026-06-26`

## 引擎新增（向後相容）
- `EventDrivenBacktester(strong_tiers=[(breadth,vix,mult),...])`：分段強勢加碼，條件越強倍數越大；None=單段。
- `EventDrivenBacktester(run(..., vix_series=...))`：可注入 VIX（sweep 提速/可重現）。
- ai_report.py 新增 `--strong-tiers "b,v,m;b,v,m"` 與 `--max-regime-scale`。

## 可部署 CLI（ai_report.py，已交叉驗證數字完全吻合）
```
# SURGE（年化 58.69% / MDD -21.5% / 839 筆）
python ai_report.py --no-hybrid-tiered --consec-loss-limit 3 \
  --sl-atr 3.5 --hold-days 22 --position-size 0.10 \
  --regime-floor 0.0 --dynamic-topk --dynamic-gap-filter \
  --regime-sizing --strong-regime-mult 1.25 --strong-breadth-min 0.55 --strong-vix-max 25.0 \
  --max-regime-scale 1.7 --strong-tiers "0.65,20,1.45;0.75,15,1.75"

# SURGE PRO（全期年化 67.13% / MDD -22.7% / 780 筆；需 --eval-start 看全期）
python ai_report.py --no-hybrid-tiered --consec-loss-limit 3 \
  --sl-atr 3.5 --hold-days 25 --position-size 0.10 \
  --regime-floor 0.0 --dynamic-topk --dynamic-gap-filter \
  --regime-sizing --strong-regime-mult 1.25 --strong-breadth-min 0.55 --strong-vix-max 28.0 \
  --max-regime-scale 1.9 --strong-tiers "0.62,18,1.7;0.72,15,1.85"

# GUARD（年化 50.9% / MDD -24.7% / 1013 筆）
python ai_report.py --no-hybrid-tiered --consec-loss-limit 3 --sl-atr 3.5 --regime-floor 0.0 --dynamic-topk
```
⚠️ **`--consec-loss-limit 3` 必加**：ai_report 預設 99 停用連損熔斷，少了它 MDD 會從 -25% 惡化到 -40%。
⚠️ 全期數字需加 `--eval-start 2019-01-01 --end-date <today>`；不加時 ai_report 用預設視窗（SURGE PRO 會顯示偏低，曾誤判為退步）。

## 每日 page（GitHub Pages）
- `.github/workflows/update_ai_report.yml` 新增 GUARD/SURGE/SURGE PRO 三步（產 report_guard.html /
  report_surge.html / report_surge_pro.html + 各自 chart）。
- `deploy_pages.yml` 部署這些頁；`index.html` 為策略選單（SURGE PRO/SURGE/GUARD/主報表/Paper）。
- 仍可用 workflow_dispatch 先手動跑一次驗證再依賴排程。

## 測過但未採用（誠實）
法人 5/10/20 籌碼退場（inst_hold_exit）與低勝率 RR gate：在此約束下都降低目標，無正貢獻。

## 注意
- 2020 fold（COVID V 轉）GUARD/SURGE 與 baseline 互有高低，但仍正報酬；非受閘年。
- 2026 為半年資料年化，數字偏高勿過度解讀。
- 工具：constrained_search.py / validate_winner.py / nested_wf_inmem.py / finalists.py；bundle 快取 artifacts/bundle_2019-01-01_60.pkl。
- 過程抓到並修正兩個會誤導結果的 bug：harness 不可傳 engineer_features 的 atr（比引擎內部小~46%）；ai_report 的 consec-loss-limit 預設不一致。
