# TW Stocker v8.5 — 完整策略分析與兵推

> 經四波 32 組 ablation 嚴格驗證的局部最優策略

## 1. 核心邏輯與進出場規則

### 選股 Universe
- 過去 20 日平均成交額 **Top-60**（嚴格流動性過濾）

### 綜合分數（唯一有效因子）
```
score = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
```
- **Momentum**：當日收盤 / 20 天前收盤（橫向百分位排名）
- **Trend**：當日收盤 / 60 日均線（橫向百分位排名）
- 其他因子（成交量爆發、穩定度、籌碼、殘差動量、趨勢品質）在消融測試中全部被證實無效或有害

### 進場條件（前日收盤產生訊號，當日開盤成交）
1. score ≥ 2.0
2. 股價 > 60MA
3. 大盤 Regime ≥ 40%（v8.5 Breadth Regime + floor 10%）
4. Gap（開盤跳空）< 1.5 × 20 日 ATR → 否則跳過
5. 每日最多 Top-7 檔（相關係數 > 0.8 時替換）
6. 板塊上限 sector cap 75%

### 大盤 Regime 漸進式曝險（最重要風控）

| 大盤位置（vs 60MA / 20MA） | 曝險比例 |
|----------------------------|:--------:|
| >60MA 且 >20MA            | 100%    |
| >60MA 且 <20MA            | 70%     |
| <60MA 且 >20MA            | 40%     |
| <60MA 且 <20MA            | 10% (floor) |

> v8.5 新增 **Breadth Regime**：用 Universe 內部寬度（Top-60 中多少比例站上 20MA）修正 0050 單一指標的盲點，避免台積電綁架大盤判斷。

### Gap-aware 倉位調整（v8.3+）

| Gap 大小     | 倉位比例 |
|--------------|:--------:|
| < 0.5 ATR   | 100%    |
| 0.5~1.0 ATR | 75%     |
| 1.0~1.5 ATR | 50%     |
| ≥ 1.5 ATR   | 跳過    |

### 出場規則
- **停損**：3 × ATR
- **停利**：4 × ATR
- **時間強制出場**：20 個交易日
- **成本**：買 0.1425% + 賣 0.4425% + 10 bps 滑價

## 2. 績效（v8.5，經消融驗證最佳配置）

| 指標 | 值 |
|------|:---:|
| **Sharpe** | **2.47** |
| **年化報酬** | **+62.5%** |
| **MDD** | **-14.2%** |
| **Calmar** | **4.40** |
| **Profit Factor** | **1.74** |
| **勝率** | **54.8%** |
| 交易數 | 562 |

## 3. 消融實驗總結（四波 32 組，全部驗證）

### 已被永久否決的功能

| 類別 | 功能 | 結果 |
|------|------|:---:|
| 歐美經典 | Frog-in-Pan（平滑度） | Sharpe 1.77 🔴 |
| 歐美經典 | Skip-month（跳過近月） | MDD -31% ☠️ |
| 歐美經典 | Absolute Momentum Gate | Sharpe 2.03 🔴 |
| 台股特性 | Limit-Up Bonus（漲停加成） | Sharpe 2.26 🔴 |
| 台股特性 | Inst Flow Gate（法人必買） | Sharpe 0.86 ☠️ |
| 台股特性 | Inst Flow Weight（籌碼加權） | Sharpe 2.08 🔴 |
| 風控工程 | MDD Breaker 8/10/15% | MDD 全部惡化 🔴 |
| 風控工程 | Vol Parity | MDD -56.4% ☠️ |
| 風控工程 | MDD + VolParity 組合 | MDD -67.1% ☠️ |
| 進場品質 | Volume Confirm（量確認） | Sharpe 2.07 🔴 |
| 進場品質 | Cluster Penalty | Sharpe 2.36 🔴 |
| 風控參數 | ConsecLoss 3/5 | Sharpe 2.19 🔴 |
| 風控參數 | Sector Cap 50% | Sharpe 1.96 ☠️ |
| 參數微調 | Universe 40/80 | 全部更差 🔴 |
| 參數微調 | TP5/SL2, TP3/SL2 | 災難 ☠️ |
| 子策略 | Dynamic Risk | Sharpe 2.33 🔴 |
| 子策略 | Mean Reversion | 無效果 |
| 子策略 | Futures Hedge | Sharpe 2.36 🔴 |

### 唯一成功升級
- **Breadth Regime + Floor 0.10** → Sharpe 2.38→2.47, MDD -15.5%→-14.2%, Calmar 4.06→4.40

## 4. 兵推：什麼情況仍會大虧？

依嚴重度排序：

### 4.1 動量崩盤（Momentum Crash）— 最致命
所有 Top-7 同時反轉，20 天內連續止損。Regime 會降曝險但 floor 10% 仍會吃到 -30%~-40% 月回撤。
- **Monte Carlo 最差 5% MDD：-44.6%**

### 4.2 大盤長期弱勢 + Floor 緩慢流血
大盤長期在 60MA 之下，floor 10% 累積小虧會放大。

### 4.3 極端跳空 + 流動性枯竭
重大利空導致已持倉跳空向下，Gap-aware 只能用 open 價止損，單日暴跌 15%+。

### 4.4 板塊高集中 + 系統性風險
Sector cap 75% 仍可能 5~6 檔同板塊，美中科技戰或 AI 泡沫破裂。

### 4.5 實盤滑價遠高於 10bps
資金規模 >1000 萬或低量日，實際滑價 30~50bps 會長期侵蝕報酬。

### 4.6 參數結構性失效
20 天動能 + 60MA 若因 AI 交易普及而失效，策略進入長期低勝率。

## 5. 實戰風控建議

### ✅ 立即可執行、經驗證不傷績效

- **外部 Portfolio Hardstop**：整體回撤 -8%~-10% 全平倉 + 暫停 10~15 天
- **每日監控**：`python paper_tracker.py` + `python paper_trade.py hardstop`
- **人工 Override**：連續 2 個月 vs 0050 月勝率 <50% 時暫停新單 1 個月
- **資金控管**：單筆風險 ≤1%，起始資金 ≥100 萬 TWD
- **地緣政治事件前**：手動降至 20% 曝險

### ❌ 絕對不要做的事

- 內建 Vol Parity、MDD Breaker、Trailing、Breakeven（已全被消融否決）
- 把 regime floor 再調低（0.10 已是最優平衡）
- 手動加碼輸錢的部位
- 未經 paper trading 驗證就投入實盤

## 6. 優化方向消融紀錄（Wave 5+6 共 16 組，全部測試完成）

### Wave 5: 宏觀 Regime (VIX) + Ensemble — 全敗

| 配置 | Sharpe | MDD | Calmar | 結論 |
|------|:---:|:---:|:---:|:---:|
| **v8.5 Baseline** | **2.47** | **-14.2%** | **4.40** | ✅ 最佳 |
| MacroRegime (VIX) | 2.46 | -14.1% | 4.33 | 🔴 |
| Macro + Floor 0.20 | 2.44 | -13.9% | 4.39 | 🔴 |
| Ensemble (F0.10+F0.00) | 2.19 | -16.9% | 3.29 | 🔴 |

### Wave 6: 分批建倉 + 動態 TopK/Gap/Corr — 全敗

| 配置 | Sharpe | MDD | Calmar | 結論 |
|------|:---:|:---:|:---:|:---:|
| Dynamic TopK | 2.48 | -14.2% | 4.41 | ⚠️ 噪音（1000d 反而更差） |
| Dynamic Gap Filter | 2.27 | -15.8% | 3.57 | 🔴 放寬跳空引入爛單 |
| Dynamic Corr Filter | 2.46 | -14.2% | 4.38 | 🔴 |
| Batch Entry 2 | 2.26 | -8.0% | 3.92 | 🔴 MDD 好但年化腰斬 |
| Batch Entry 3 | 2.21 | -6.6% | 3.82 | 🔴 同上 |
| DynTopK + DynGap | 2.14 | -14.3% | 3.63 | 🔴 |
| All 3 Dynamic | 2.12 | -14.3% | 3.61 | 🔴 |
| All 3 + Batch3 | 1.94 | -6.6% | 3.21 | ☠️ |

### 僅存的安全探索方向

1. **台指期隱含波動率**：比 VIX 更在地化（需付費數據）
2. **月營收動能**：需可靠 API
3. **低相關第二策略引擎**：均值回歸或事件驅動（需獨立邏輯）

> ⚠️ 任何優化必須先跑 `walk_forward.py` + `monte_carlo.py` 驗證。

---

*最後更新：2026-04-07 | 策略版本：v8.5 (Ablation-Proven Local Optimum)*
*消融實驗總計：六波 50+ 組配置，v8.5 全面最優*

