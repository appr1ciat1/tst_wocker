# TW Stocker v8 — AI 量化交易系統

中期橫截面動量策略，流動性 Universe 排名 + 事件驅動回測 + ATR 停利停損。
經 Walk-Forward 驗證、100+ 組參數掃描、2000 次 Block Bootstrap Monte Carlo 壓力測試。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html

## 績效總覽（v8.2 final — 無 lookahead + graduated regime + sector cap）

| 指標 | 值 | 說明 |
|------|:---:|------|
| **Sharpe** | **2.30** | 無 lookahead + 10bps 滑價 + graduated regime + sector 60% |
| **年化報酬** | **+62.7%** | 包含交易成本 + 滑價 |
| **MDD** | **-16.9%** | 四段式曝險 + 板塊分散控管左尾 |
| **Calmar** | **3.72** | 年化報酬/MDD |
| **Profit Factor** | **1.82** | 總獲利/總虧損 |
| **勝率** | **57.4%** | 507 筆交易 |

### 演化歷程

| 版本 | Sharpe | 年化 | MDD | Calmar | 說明 |
|------|:------:|:----:|:---:|:------:|------|
| v7 (lookahead) | 2.96 | +91% | -18% | 4.96 | ⚠️ 含 lookahead bias |
| v8.1 (honest) | 1.95 | +61% | -30% | 2.02 | ✅ 修正 lookahead |
| v8.2 (graduated) | 2.21 | +64% | -19% | 3.39 | ✅ + graduated regime |
| **v8.2 (final)** | **2.30** | **+63%** | **-17%** | **3.72** | ✅ + sector cap 60% |

> v7→v8.1：修正 regime filter 和 volume confirm 的 lookahead bias。
> v8.1→v8.2：四段式曝險 + Universe 60 + Top-7 + 板塊分散 60%。

### Monte Carlo 壓力測試（Block Bootstrap, 2000x, v8.2 honest）

| 情境 | 最差 5% 報酬 | 最差 5% MDD | 中位數報酬 |
|------|:----------:|:-----------:|:--------:|
| 全體 (block=5) | +178.5% | -22.0% | +400.3% |
| 保守 (勝率50%) | **+1.0%** | -44.6% | +135.0% |

> ✅ 保守情境（勝率降至 50%）最差 5% 報酬仍為正，代表策略有足夠安全邊際。
> 預期實盤 MDD：-13% ~ -22%，建議起始資金 ≥ 45 萬。

## 策略公式

```
每日訊號:
  1. Universe = 過去 20 日平均成交額 Top-60
  2. 綜合評分 = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
  3. 進場: score ≥ 2.0 AND close > 60MA AND 大盤 regime ≥ 40%
  4. 跳空 > 1.5×ATR 的進場日跳過
  5. Top-7 選股（相關性 > 0.8 的替換為不相關候選）

Graduated Regime (v8.2):
  - 大盤 > 60MA + > 20MA → 100% 曝險
  - 大盤 > 60MA + < 20MA →  70% 曝險（轉弱）
  - 大盤 < 60MA + > 20MA →  40% 曝險（轉強）
  - 大盤 < 60MA + < 20MA →   0% 停止進場

出場 (gap-aware):
  - 停損: min(stop_price, open)  ← 隔夜跳空用開盤價
  - 停利: max(tp_price, open)    ← 跳空有利用開盤價
  - 時間: 20 個交易日強制出場

成本: 買 0.1425% + 賣 0.4425% + 滑價 10bps
```

## 快速開始

```bash
pip install -r requirements.txt

# v8 誠實回測 + 籌碼標注
python ai_report.py --show-inst

# 籌碼因子加權測試（建議累積 2 年數據後再啟用）
python ai_report.py --inst-flow 0.5 --show-inst

# Paper Trading v8
python paper_trade.py signals --enrich    # 籌碼 + 新聞標注
python paper_trade.py hardstop             # 組合 hard stop
python paper_trade.py monthly              # 月報

# Block Bootstrap 壓力測試
python monte_carlo.py --runs 2000 --block-size 5
```

## CLI 參數

### 核心（已鎖定）
| 參數 | 預設值 | 說明 |
|---|:---:|---|
| `--tp-atr` | `4.0` | ATR 停利倍數 |
| `--sl-atr` | `3.0` | ATR 停損倍數 |
| `--top-k` | `7` | 每日最多進場股票數 |
| `--hold-days` | `20` | 最大持倉交易日 |
| `--gap-filter` | `1.5` | 跳空過濾 ATR 倍數 |
| `--universe-size` | `60` | 動態流動性 Universe 大小 |
| `--regime-filter` | `true` | 大盤過濾 (0050 > 60MA) |
| `--regime-graduated` | `true` | 四段式曝險 (100/70/40/0%) |
| `--slippage` | `0.001` | 滑價 10bps |

### 可選風控（opt-in, 經實測驗證效果）
| 參數 | 預設值 | 實測結果 |
|---|:---:|---|
| `--sector-max-pct` | `1.0` | 0.5 可壓 MDD 至 -16.0% (Sharpe 持平) |
| `--corr-filter` | `0.8` | 去除高度相關持倉 |
| `--rank-weight` | `false` | 有害: Sharpe -27% |
| `--regime-delev` | `false` | 有害: Sharpe -32%, 錯過反彈 |
| `--dynamic-risk` | `false` | 中性: Sharpe ±2% |
| `--inst-flow` | `0.0` | 籌碼因子權重（累積數據中，建議先用 0） |
| `--show-inst` | `true` | 報表信號顯示籌碼/新聞標注 |

### 已驗證無效（永久排除）
| 功能 | 影響 |
|------|------|
| `--breakeven` | Sharpe → 0.48 ☠️ |
| `--trailing` | Sharpe → ~0.08 ☠️ |
| `--ml-weights` | Sharpe -55% |
| `--rank-weight` | Sharpe -27% |

## v8.1 回測誠實化 — Lookahead 修正

### 修正 1：Regime Filter Lookahead（影響最大）
```
v7: market_close[date] > market_ma60[date]  ← 用同日收盤決定同日開盤進場
v8: market_close[i-1] > market_ma60[i-1]    ← 只用昨日資訊
```
影響：Sharpe 2.96 → 1.95, MDD -18.4% → -30.0%（大盤轉弱時多了很多錯誤進場）

### 修正 2：Volume Confirm Lookahead
```
v7: vol_df[ticker].iloc[i] > vol_ma20[i]    ← 用同日成交量
v8: vol_df[ticker].iloc[i-1] > vol_ma20[i-1] ← 用昨日成交量
```

### 修正 3：Constructor Defaults 對齊
```
v7 backtester defaults: tp_atr=3.0, sl_atr=2.0, hold=30, gap=0, top_k=3
v8 backtester defaults: tp_atr=4.0, sl_atr=3.0, hold=20, gap=1.5, top_k=5  ← 對齊 README
```

### 修正 4：Ablation Study 對齊
```
v7 ablation: tp_atr=3.0, sl_atr=1.5, days=800, top_k=3, no regime filter
v8 ablation: tp_atr=4.0, sl_atr=3.0, days=1200, top_k=5, regime filter  ← 對齊 README
```

## v8 新功能 — 籌碼因子 + Paper Trading 強化

### 三大法人籌碼整合
- 數據來源: [tw-institutional-stocker](https://github.com/voidful/tw-institutional-stocker)
- 因子: `three_inst_ratio_change_20`（20 日持股變化 %）
- 當前狀態: **標注模式**（weight=0）— 報表中顯示但不影響選股分數
- 未來規劃: 累積 2 年數據後做 ablation，決定是否加入評分公式

### Paper Trading v8
| 命令 | 說明 |
|------|------|
| `signals --enrich` | 信號 + 籌碼/新聞標注 |
| `hardstop` | 組合權益保護 (soft -10% / hard -15%) |
| `monthly` | 月度績效報告 (Markdown) |
| `alert` | 回測回撤警報 |

## 專案結構

```
tw_stocker/
├── ai_report.py              # 主程式 + CLI + HTML 報表 (v8)
├── sweep.py                  # 季度參數校準 + 劣化警報 + Telegram
├── walk_forward.py           # Walk-Forward 穩定性驗證
├── monte_carlo.py            # Block Bootstrap 壓力測試 v3
├── paper_trade.py            # Paper Trading v8 + 籌碼標注 + 月報
├── strategy/
│   ├── ai_strategy.py        # 因子工程 (Mom×3 + Trend×1 + Inst×W)
│   ├── event_backtest.py     # 事件驅動回測 + gap-aware fill + 風控
│   ├── institutional_flow.py # 三大法人籌碼因子 (v8 新增)
│   ├── news_sentiment.py     # 新聞情緒因子 (v8 新增)
│   ├── risk_metrics.py       # 風險指標計算
│   └── benchmark.py          # Benchmark (0050 / EW)
├── artifacts/                # 每日 CSV + 月報
├── .github/workflows/
│   └── update_ai_report.yml  # 每日 + 月報 + 季度自動執行 (v8)
└── stock_report.html         # 完整交易報表 (v8)
```

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
