# TW Stocker v6 — AI 量化交易系統

每日自動更新的 AI 台股動量策略，經嚴格 Walk-Forward 驗證、100+ 組參數掃描、2000 次 Monte Carlo 壓力測試。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html

## 績效總覽

| 指標 | 值 | 說明 |
|------|:---:|------|
| **Sharpe** | **2.96** | 風險調整報酬（>2 為優秀） |
| **年化報酬** | **+90.8%** | 包含交易成本 |
| **MDD** | **-16.7%** | 最大權益回撤 |
| **Calmar** | **5.44** | 年化報酬/MDD（>3 為優秀） |
| **Profit Factor** | **2.26** | 總獲利/總虧損 |
| **勝率** | **60.5%** | 470 筆交易 |
| **α vs 0050** | **+49.3%** | 年化超額報酬 |

### Walk-Forward 穩定性

| 窗口 | Sharpe | 年化 | MDD |
|------|:------:|:----:|:---:|
| 1500d | 2.29 | +64% | -17% |
| 1200d | 2.96 | +91% | -17% |
| 900d | 1.60 | +48% | -27% |
| 600d | 3.46 | +112% | -16% |
| **穩定性比** | **3.22** | *(> 3 = 優秀)* | |

### Monte Carlo 壓力測試 (2000 次)

| 情境 | 最差 5% 報酬 | 最差 5% MDD |
|------|:----------:|:-----------:|
| 全體 | +176% | -12.5% |
| 保守 (勝率50%) | +30% | -18.8% |

## 快速開始

```bash
pip install -r requirements.txt

# 產出每日報表（v6 最佳配置 = corr-filter 0.8）
python ai_report.py

# 完整壓力測試
python monte_carlo.py --runs 2000
```

## 策略公式

```
每日訊號:
  1. Universe = 過去 20 日平均成交額 Top-50
  2. 綜合評分 = rank_momentum(20d) × 3 + rank_trend(60MA) × 1
  3. 進場: score ≥ 2.0 AND close > 60MA AND 大盤 > 60MA
  4. 跳空 > 1.5×ATR 的進場日跳過
  5. Top-5 選股（相關性 > 0.8 的替換為不相關候選）

出場:
  - 停利: entry + 4.0×ATR
  - 停損: entry - 3.0×ATR
  - 時間: 20 個交易日強制出場

成本: 買 0.1425% + 賣 0.4425%
```

## 工具箱

### 每日流程

```bash
# 17:00 後：產出報表 + Telegram 推送
python ai_report.py
python paper_trade.py signals --notify

# 隔日開盤後：記錄實際成交
python paper_trade.py log --ticker 2330 --action buy --price 980 --shares 1000

# 每週五：檢查執行率 + 滑價（自動 Telegram 警報）
python paper_trade.py report

# 回撤警報
python paper_trade.py alert --max-dd 15
```

### 季度維護

```bash
# 快速參數校準（~4 分鐘）
python sweep.py --quick

# Walk-Forward 驗證
python walk_forward.py

# Monte Carlo 壓力測試（regime-aware）
python monte_carlo.py --runs 2000
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
| `--corr-filter` | `0.8` | 去除高度相關持倉 |

### 可選功能（opt-in）
| 參數 | 預設值 | 說明 |
|---|:---:|---|
| `--sector-max-pct` | `1.0` | 板塊集中上限 (建議 0.5) |
| `--dynamic-risk` | `false` | 動態風險預算 |
| `--dd-pause-pct` | `0.10` | 回撤暫停門檻 |
| `--consec-loss-limit` | `3` | 連損暫停筆數 |
| `--slippage` | `0` | 滑價模型 (0.001 = 0.1%) |

## 風險控管

### 8 層防護機制
| 層級 | 機制 | 狀態 |
|------|------|:----:|
| 1 | **Regime Filter** — 大盤 < 60MA 禁入場 | 預設 ON |
| 2 | **ATR×3.0 停損** — 單筆限虧 ~9% | 預設 ON |
| 3 | **Gap Filter** — 跳空 > 1.5×ATR 跳過 | 預設 ON |
| 4 | **相關性過濾** — 去除 corr > 0.8 重複持倉 | 預設 ON |
| 5 | **時間止損** — 20 天強制出場 | 預設 ON |
| 6 | **Sweep 警報** — Sharpe < 1.8 或 MDD > 22% | 自動 |
| 7 | **Telegram 警報** — 執行率 / 滑價 / 回撤 | 自動 |
| 8 | **Sector Cap** — 電子股集中保護 | opt-in |

### 退出條件（自動監控）
- `sweep.py` 報 Sharpe < 1.8 → Telegram 警報 + CI 阻擋
- 單月 MDD > -12% → Telegram 即時通知
- 連續 2 月執行率 < 85% → 檢查掛單方式
- Monte Carlo 最差 5% MDD > -22% → 暫停新倉

## 已驗證無效功能（永久排除）

| 功能 | 影響 |
|------|------|
| `--breakeven` | Sharpe 2.96 → 0.48 ☠️ |
| `--trailing` | Sharpe → ~0.08 ☠️ |
| `--ml-weights` | Sharpe -55% |
| `--blacklist 3` | Sharpe 2.96 → 2.58 (-13%) |
| Circuit Breaker 10% | MDD 反增至 -33% |

## 專案結構

```
tw_stocker/
├── ai_report.py              # 主程式 + CLI + HTML 報表 (v5)
├── sweep.py                  # 季度自動參數校準 + 劣化警報
├── walk_forward.py           # Walk-Forward 穩定性驗證
├── monte_carlo.py            # Monte Carlo 壓力測試 v2 (regime-aware)
├── paper_trade.py            # Paper Trading + Telegram 通知
├── strategy/
│   ├── ai_strategy.py        # 因子工程 (Mom×3 + Trend×1)
│   ├── event_backtest.py     # 事件驅動回測引擎 + 風險防護
│   ├── risk_metrics.py       # 風險指標計算
│   └── benchmark.py          # Benchmark (0050 / EW)
├── artifacts/                # 每日 CSV (trades/equity/signals)
├── .github/workflows/
│   └── update_ai_report.yml  # 每日 + 季度自動執行
├── stock_report.html         # 完整交易報表 (v5 含 7 個診斷區塊)
└── backtest_chart.png        # 資金曲線圖
```

## GitHub Actions

- **每日**：UTC 09:00（台灣 17:00）自動生成報表
- **季度**：1/4/7/10 月第一週自動跑 sweep + walk-forward + Monte Carlo
- **手動**：支援 `workflow_dispatch` 自訂參數
- **劣化警報**：exit code 1 = CI 自動阻擋 + Telegram 通知

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
