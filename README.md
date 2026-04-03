# TW Stocker — AI 量化區間交易系統

每日自動更新的 AI 台股量化交易系統，提供停利 / 停損 / 時間出場的完整區間交易計畫。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html

## 核心功能

| 功能 | 說明 |
|---|---|
| **AI 多因子排名** | 四維度弱指標橫向百分位排名，動態適應全天候市場 |
| **區間停利 (TP)** | 盤中最高價觸碰目標價即獲利了結 |
| **絕對停損 (SL)** | 盤中最低價跌破防守價即砍倉 |
| **時間出場 (Time Exit)** | 持有超過 N 天強制以收盤價平倉 |
| **精確 High/Low 回測** | 使用每日最高/最低價，貼近實盤結果 |
| **HTML 實戰報表** | OCO 智慧掛單建議，直接照表操課 |

## 快速開始

```bash
# 安裝依賴
pip install yfinance pandas numpy matplotlib

# 使用預設參數（14 檔熱門股、TP +15%、SL -8%、持倉 20 天）
python ai_report.py

# 自訂參數
python ai_report.py --tickers 2330 2317 2454 \
                    --tp 0.15 \
                    --sl 0.08 \
                    --hold-days 20 \
                    --days 800

# 查看所有參數
python ai_report.py --help
```

## CLI 參數說明

| 參數 | 預設值 | 說明 |
|---|---|---|
| `--tickers` | 14 檔熱門股 | 股池代號列表（空格分隔） |
| `--tp` | `0.15` | 停利百分比 (+15%) |
| `--sl` | `0.08` | 停損百分比 (-8%) |
| `--hold-days` | `20` | 最大持倉交易日 |
| `--days` | `800` | 歷史回測天數 |
| `--threshold` | `3.2` | AI 評分進場門檻 (滿分 4.0) |
| `--capital` | `1000000` | 初始模擬資金 |

## AI 四維度排名指標

1. **20 日動能 (Momentum)** — 近期漲幅強度
2. **60MA 乖離率 (Trend Bias)** — 偏離均線程度
3. **5/20 日量能比 (Volume Surge)** — 短期量能放大倍率
4. **20 日波動率倒數 (Stability)** — 越穩定排名越高

四個指標各自做「橫向百分位排名」（同一天比較所有股票），等權加總為 0~4 分的綜合評分。評分 ≥ 3.2 且收盤價 > 60MA 時觸發買進信號。

## 策略紀律

- **盈虧比 ≈ 1:2**：停利 +15% vs 停損 -8%（大賺小賠）
- **不抱死魚股**：持有滿 20 個交易日強制出場，釋放資金
- **資金控管**：每次進場投入總資金 10%，最多同時持有 10 檔

## 專案結構

```
tw_stocker/
├── ai_report.py                 # 主程式 + CLI + HTML 報表產生
├── strategy/
│   ├── ai_strategy.py           # AI 特徵工程 + yfinance 資料下載
│   └── event_backtest.py        # 事件驅動回測引擎 (TP/SL/Time)
├── data/                        # 歷史 CSV 資料（自動更新）
├── .github/workflows/
│   └── update_ai_report.yml     # 週一~五台灣 17:00 自動執行
├── stock_report.html            # 產出的 AI 交易計畫報表
└── backtest_chart.png           # 產出的資金曲線圖
```

## GitHub Actions 自動化

系統透過 GitHub Actions 每日自動執行：
- **排程**：週一到五 UTC 09:00（台灣時間 17:00 收盤後）
- **手動觸發**：支援 `workflow_dispatch`，可在 GitHub 網頁上自訂 TP / SL / 股池參數
- **輕量維護**：搭配 orphan commit 策略，repo 永遠只保留最新一個 commit

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
