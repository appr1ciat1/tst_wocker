# TW Stocker v2 — AI 量化交易研究系統

每日自動更新的 AI 台股量化交易系統，完整風險報告、Benchmark 對比、OCO 智慧掛單建議。

📊 **線上報表**：https://voidful.github.io/tw_stocker/stock_report.html

## v2 核心改進

| 改進項目 | v1 | v2 |
|---|---|---|
| **Entry** | 昨日信號 → 今日收盤進場 | 昨日信號 → 今日**開盤**進場（對齊實盤） |
| **股池** | 固定 14 檔 | 動態 Liquid Universe（全 TWSE Top-N） |
| **選股** | 固定 threshold ≥ 3.2 | **Top-K** 排名選股 + 安全下限 |
| **TP/SL** | 固定 +15% / -8% | **ATR 自適應**（倍數可調） |
| **Sizing** | 固定 initial_capital × 10% | **Current equity** × 10% |
| **成本** | 無 | 手續費 0.1425% + 證交稅 0.3% |
| **風險報告** | 總報酬、勝率 | Sharpe/Sortino/MaxDD/Calmar/Profit Factor |
| **Benchmark** | 無 | 0050 Buy & Hold + Equal-Weight 對比 |
| **出場日** | calendar day × 1.4 近似 | **exchange_calendars** 精確交易日 |
| **歷史保留** | orphan commit（只留最新） | 正常 commit + artifacts/ 時序 CSV |

## 核心功能

| 功能 | 說明 |
|---|---|
| **AI 多因子排名** | 四維度弱指標橫向百分位排名，動態適應全天候市場 |
| **ATR 自適應 TP/SL** | 波動度驅動的停利/停損，對不同波動結構更公平 |
| **區間停利 (TP)** | 盤中最高價觸碰目標價即獲利了結 |
| **絕對停損 (SL)** | 盤中最低價跌破防守價即砍倉 |
| **時間出場** | 持有超過 N 個交易日強制以收盤價平倉 |
| **交易成本建模** | 完整台股手續費 + 證交稅扣減 |
| **精確 High/Low 回測** | 使用每日最高/最低價，貼近實盤結果 |
| **風險報表** | Sharpe / Sortino / MaxDD / Calmar / Profit Factor |
| **Benchmark 對比** | vs 0050 ETF Buy & Hold |
| **因子 Ablation** | 獨立腳本分析各因子邊際貢獻 |

## 快速開始

```bash
# 安裝依賴
pip install -r requirements.txt

# v2 預設（動態 Universe, ATR TP/SL, Top-3 選股）
python ai_report.py

# 使用靜態 14 檔股池（兼容 v1 模式）
python ai_report.py --static-pool

# 自訂參數
python ai_report.py --universe-size 100 \
                    --top-k 5 \
                    --tp-sl-mode atr \
                    --tp-atr 3.0 \
                    --sl-atr 1.5 \
                    --hold-days 20 \
                    --days 800

# 固定百分比 TP/SL 模式
python ai_report.py --tp-sl-mode fixed --tp 0.15 --sl 0.08

# 因子 Ablation Study
python ablation_study.py

# 查看所有參數
python ai_report.py --help
```

## CLI 參數

| 參數 | 預設值 | 說明 |
|---|---|---|
| `--universe-size` | `50` | 動態 Universe 大小 (Top-N 流動性) |
| `--static-pool` | `false` | 使用靜態 14 檔股池 |
| `--tickers` | 擴展池 | 手動指定股池（配合 --static-pool） |
| `--tp-sl-mode` | `atr` | TP/SL 模式：`atr` 或 `fixed` |
| `--tp-atr` | `3.0` | ATR 停利倍數 |
| `--sl-atr` | `1.5` | ATR 停損倍數 |
| `--tp` | `0.15` | 固定模式停利百分比 |
| `--sl` | `0.08` | 固定模式停損百分比 |
| `--top-k` | `3` | 每日最多進場股票數 |
| `--threshold` | `2.0` | AI 評分安全下限 |
| `--hold-days` | `20` | 最大持倉交易日 |
| `--capital` | `1000000` | 初始模擬資金 |
| `--position-size` | `0.10` | 每筆倉位佔當前權益比例 |
| `--buy-cost` | `0.001425` | 買入手續費率 (0.1425%) |
| `--sell-cost` | `0.004425` | 賣出成本率 (0.1425% + 0.3% 稅) |
| `--days` | `800` | 歷史回測天數 |

## AI 四維度排名指標

1. **20 日動能 (Momentum)** — 近期漲幅強度
2. **60MA 乖離率 (Trend Bias)** — 偏離均線程度
3. **5/20 日量能比 (Volume Surge)** — 短期量能放大倍率
4. **20 日波動率倒數 (Stability)** — 越穩定排名越高

四個指標各自做「橫向百分位排名」（同一天在 Universe 中比較），等權加總為 0~4 分。
使用 Top-K 選股（每日取前 K 名），並保留 close > 60MA 趨勢濾網。

## 策略紀律

- **ATR 自適應盈虧比**：TP/SL 隨波動度調整，對不同標的更公平
- **不抱死魚股**：持有滿 20 個交易日強制出場，釋放資金
- **資金控管**：每次進場投入**當前權益** 10%，最多同時持有 10 檔
- **含交易成本**：買 0.1425% 手續費 + 賣 0.1425% 手續費 + 0.3% 證交稅

## 專案結構

```
tw_stocker/
├── ai_report.py                 # 主程式 + CLI + HTML 報表產生
├── ablation_study.py            # 因子 Ablation Study
├── strategy/
│   ├── ai_strategy.py           # AI 特徵工程 + 動態 Universe
│   ├── event_backtest.py        # 事件驅動回測引擎 (ATR TP/SL + 成本)
│   ├── risk_metrics.py          # 風險指標計算
│   └── benchmark.py             # Benchmark 對比 (0050 / EW)
├── artifacts/                   # 每日 CSV 輸出 (trades/equity/signals)
├── data/                        # 歷史 5 分鐘 CSV 資料
├── .github/workflows/
│   └── update_ai_report.yml     # 週一~五台灣 17:00 自動執行
├── stock_report.html            # 產出的交易計畫報表 (含風險+Benchmark)
└── backtest_chart.png           # 產出的資金曲線圖 (含 Drawdown)
```

## GitHub Actions 自動化

系統透過 GitHub Actions 每日自動執行：
- **排程**：週一到五 UTC 09:00（台灣時間 17:00 收盤後）
- **手動觸發**：支援 `workflow_dispatch`，可自訂 TP/SL 模式、Universe 大小、Top-K
- **歷史保留**：正常 commit 保留完整版本歷史
- **自動清理**：artifacts/ 目錄保留最近 180 天

## 免責聲明

本系統由 AI 量化模型自動產出，僅供學術研究與技術交流之用，不構成任何投資建議。歷史回測績效不代表未來實際報酬，投資有風險，決策請自行負責。
