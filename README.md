# TW Stocker v5 — AI 量化交易系統

每日自動更新的 AI 台股動量策略，經嚴格 Walk-Forward 驗證與 100+ 組參數掃描。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html

## 績效總覽

| 指標 | 值 | 說明 |
|------|:---:|------|
| **Sharpe** | **2.81** | 風險調整報酬（>2 為優秀） |
| **年化報酬** | **+85.7%** | 包含交易成本 |
| **MDD** | **-16.8%** | 最大權益回撤 |
| **Calmar** | **5.11** | 年化報酬/MDD（>3 為優秀） |
| **勝率** | **60.9%** | 478 筆交易 |
| **α vs 0050** | **+43.1%** | 年化超額報酬 |

### Walk-Forward 穩定性

| 窗口 | Sharpe | 年化 | MDD |
|------|:------:|:----:|:---:|
| 1500d | 2.29 | +64% | -17% |
| 1200d | 2.81 | +86% | -17% |
| 900d | 1.60 | +48% | -27% |
| 600d | 3.46 | +112% | -16% |
| **穩定性比** | **3.22** | *(> 3 = 優秀)* | |

## 快速開始

```bash
pip install -r requirements.txt

# 產出每日報表（零參數即最佳配置）
python ai_report.py

# 保守版（動態風險預算）
python ai_report.py --dynamic-risk
```

## 策略公式

```
每日訊號:
  1. Universe = 過去 20 日平均成交額 Top-50
  2. 綜合評分 = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
  3. 進場: score ≥ 2.0 AND close > 60MA AND 大盤 > 60MA
  4. 跳空 > 1.5×ATR 的進場日跳過
  5. Top-5 選股

出場:
  - 停利: entry + 4.0×ATR
  - 停損: entry - 3.0×ATR
  - 時間: 20 個交易日強制出場

成本: 買 0.1425% + 賣 0.4425%
```

## 工具箱

### 日常使用

```bash
# 產出報表 + 信號
python ai_report.py

# Paper Trading: 擷取今日信號
python paper_trade.py signals

# Paper Trading: 記錄實際成交
python paper_trade.py log --ticker 2330 --action buy --price 980 --shares 1000

# Paper Trading: 月度比對報告
python paper_trade.py report
```

### 季度維護

```bash
# 快速參數校準（~4 分鐘）
python sweep.py --quick

# 完整參數校準 + CSV 記錄（~12 分鐘）
python sweep.py --output csv

# Walk-Forward 驗證（~2 分鐘）
python walk_forward.py
```

## CLI 參數

### 核心（已鎖定最優）
| 參數 | 預設值 | 說明 |
|---|:---:|---|
| `--tp-atr` | `4.0` | ATR 停利倍數 |
| `--sl-atr` | `3.0` | ATR 停損倍數 |
| `--top-k` | `5` | 每日最多進場股票數 |
| `--hold-days` | `20` | 最大持倉交易日 |
| `--gap-filter` | `1.5` | 跳空過濾 ATR 倍數 |
| `--regime-filter` | `true` | 大盤過濾 (0050 > 60MA) |
| `--days` | `1200` | 歷史回測天數 |

### 可選功能
| 參數 | 預設值 | 說明 |
|---|:---:|---|
| `--dynamic-risk` | `false` | 動態風險預算（根據市場波動調整部位） |
| `--mean-reversion` | `false` | 均值回歸子策略（熊市超跌反彈） |
| `--futures-hedge` | `false` | 台指期空單模擬（熊市對沖） |
| `--slippage` | `0` | 滑價模型（0.001 = 0.1%） |
| `--position-size` | `0.10` | 每筆倉位佔權益比例 |
| `--universe-size` | `50` | 動態 Universe 大小 |

## 風險控管

### 內建機制
- **單筆風險**：每筆 = 權益 × 10%，最多同時 10 檔
- **Regime Filter**：大盤 < 60MA 時暫停所有進場
- **Gap Filter**：開盤跳空 > 1.5×ATR 時跳過
- **時間止損**：持倉超過 20 交易日強制出場

### 退出條件（建議手動監控）
- 連續 2 季 sweep.py 報 Sharpe < 1.8 → 暫停實盤
- 單月 MDD > -12% → 次月減半部位
- sweep.py exit code = 1 → 策略已劣化，需人工介入

## 已驗證無效功能（請勿啟用）

| 功能 | 影響 |
|------|------|
| `--breakeven` | Sharpe 2.81 → 0.48 ☠️ |
| `--trailing` | Sharpe → ~0.08 ☠️ |
| `--ml-weights` | Sharpe -55% |
| `--mean-reversion` | Sharpe -20% |

## 專案結構

```
tw_stocker/
├── ai_report.py              # 主程式 + CLI + HTML 報表
├── sweep.py                  # 季度自動參數校準
├── walk_forward.py           # Walk-Forward 穩定性驗證
├── paper_trade.py            # Paper Trading 比對工具
├── strategy/
│   ├── ai_strategy.py        # 因子工程 (Mom×3 + Trend×1)
│   ├── event_backtest.py     # 事件驅動回測引擎
│   ├── risk_metrics.py       # 風險指標計算
│   └── benchmark.py          # Benchmark (0050 / EW)
├── artifacts/                # 每日 CSV (trades/equity/signals)
├── .github/workflows/
│   └── update_ai_report.yml  # 每日 UTC 09:00 自動執行
├── stock_report.html         # 交易報表
└── backtest_chart.png        # 資金曲線圖
```

## GitHub Actions

- **排程**：UTC 09:00（台灣 17:00 收盤後）每日自動執行
- **手動觸發**：支援 `workflow_dispatch` 自訂參數
- **劣化警報**：sweep.py exit code 1 = 策略需重新校準

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
