#!/usr/bin/env python3
"""
build_paper_page.py — 乾淨的「四策略」Paper / 績效比較頁產生器（取代舊 v9 paper_trading.html）

跑四個正式註冊策略（v8.5 / GUARD / SURGE / SURGE PRO）全期回測，產出：
- 一張正確的折線圖（chart.js，log 軸）：四條權益曲線（各自起點 normalize 為 100）
- 四策略摘要表：年化 / Sharpe / MDD / Calmar / 交易數
- 當日最強策略（SURGE PRO）的買入訊號

完全不含 v9 Hybrid Tiered / Core-Satellite 內容。資料只下載一次（共用 MarketData）。
"""

import glob
import json
import os
import re
from datetime import date

import pandas as pd

from strategies.registry import get_strategy
from strategies.base import ExecConfig
from twstk.backtest.engine import RunConfig, build_market_data
from twstk.backtest.metrics import compute_risk_metrics

HTML_FILE = "paper_trading.html"
CAPITAL = 1_000_000

# (顯示名, 註冊名, 顏色, 一句說明)
STRATS = [
    ("SURGE PRO", "mom_surge_pro", "#fda4af", "去風險 + 更激進分段加碼，報酬最高"),
    ("SURGE",     "mom_surge",     "#fdba74", "去風險 + 分段強勢加碼"),
    ("GUARD",     "mom_guard",     "#5eead4", "弱勢去風險，不加碼，最穩健"),
    ("v8.5",      "momentum_v85",  "#a5b4fc", "純動量基準（優化前）"),
]


def _downsample(dates, values, step):
    if step <= 1:
        return dates, values
    return dates[::step], values[::step]


def run_all():
    cfg = RunConfig(tickers=None, days=3000, start_date="2019-01-01", end_date=None,
                    universe_size=60, initial_capital=CAPITAL, top_k=7, threshold=2.0)
    print("⏬ 下載資料（一次，四策略共用）...")
    data = build_market_data(cfg, get_strategy("momentum_v85"))
    exec_cfg = ExecConfig(initial_capital=CAPITAL, top_k=7, threshold=2.0)

    out = []
    for disp, reg, color, desc in STRATS:
        print(f"▶ 回測 {disp} ({reg}) ...")
        strat = get_strategy(reg)
        trades, equity = strat.run_engine(data, exec_cfg)
        m = compute_risk_metrics(equity, trades, CAPITAL)
        eq = (equity["Equity"] if "Equity" in equity.columns else equity.iloc[:, 0]).sort_index()
        eq = eq.dropna()
        norm = (eq / eq.iloc[0] * 100.0)
        dates = [d.strftime("%Y-%m-%d") for d in norm.index]
        vals = [round(float(v), 2) for v in norm.values]
        # 控制點數（chart.js 流暢）：>900 點則抽樣
        step = max(1, len(vals) // 900)
        dts, vs = _downsample(dates, vals, step)
        out.append({
            "disp": disp, "reg": reg, "color": color, "desc": desc,
            "ann": m.get("ann_return", 0), "sharpe": m.get("sharpe", 0),
            "mdd": m.get("max_drawdown_pct", 0), "calmar": m.get("calmar", 0),
            "trades": m.get("total_trades", 0), "win": m.get("win_rate", 0),
            "dates": dts, "vals": vs,
        })
        print(f"   {disp}: ann={m.get('ann_return',0)*100:.1f}% MDD={m.get('max_drawdown_pct',0)*100:.1f}% "
              f"Sharpe={m.get('sharpe',0):.2f} 交易={m.get('total_trades',0)}")
    return out


def today_signals():
    """讀最新 artifacts/orders_*.json（= SURGE PRO，最後跑）→ 今日買入訊號。"""
    files = sorted(glob.glob("artifacts/orders_*.json"))
    if not files:
        return None, []
    latest = files[-1]
    try:
        payload = json.load(open(latest, encoding="utf-8"))
    except Exception:
        return latest, []
    sigs = []
    for o in payload.get("orders", []):
        if o.get("side") != "buy":
            continue
        sigs.append({
            "ticker": o.get("ticker"),
            "entry": o.get("limit_price") or o.get("reference_close"),
            "tp": o.get("tp_price"), "sl": o.get("sl_price"),
            "exec": o.get("execution_date"),
        })
    return latest, sigs


def build_html(results, sig_file, signals):
    today = date.today().strftime("%Y-%m-%d")
    # 摘要表
    rows = ""
    for r in results:
        rows += (
            f"<tr><td><b style='color:{r['color']}'>{r['disp']}</b><br>"
            f"<span style='color:#94a3b8;font-size:.8rem'>{r['reg']} · {r['desc']}</span></td>"
            f"<td>{r['ann']*100:+.1f}%</td><td>{r['sharpe']:.2f}</td>"
            f"<td>{r['mdd']*100:.1f}%</td><td>{r['calmar']:.2f}</td>"
            f"<td>{r['trades']}</td><td>{r['win']*100:.0f}%</td></tr>"
        )
    # 圖表 datasets
    labels = json.dumps(results[0]["dates"], ensure_ascii=False)
    datasets = []
    for r in results:
        datasets.append(
            "{label:%s,data:%s,borderColor:'%s',backgroundColor:'transparent',"
            "fill:false,tension:0.2,pointRadius:0,borderWidth:2}"
            % (json.dumps(r["disp"]), json.dumps(r["vals"]), r["color"])
        )
    datasets_js = "[" + ",".join(datasets) + "]"
    # 訊號
    if signals:
        sig_rows = "".join(
            f"<tr><td>{s['ticker']}</td><td>{s['entry']}</td><td>{s['tp']}</td>"
            f"<td>{s['sl']}</td><td>{s['exec'] or '-'}</td></tr>" for s in signals[:20]
        )
        sig_html = (
            f"<p style='color:#94a3b8'>來源：{os.path.basename(sig_file or '')}（SURGE PRO 最強策略當日計畫）</p>"
            "<table><tr><th>股票</th><th>參考進場</th><th>停利</th><th>停損</th><th>執行日</th></tr>"
            f"{sig_rows}</table>"
        )
    else:
        sig_html = "<p style='color:#94a3b8'>今日無新買入訊號（或訂單檔尚未產生）。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-TW"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>四策略績效比較 — {today}</title>
<meta name="description" content="v8.5 / GUARD / SURGE / SURGE PRO 四策略全期權益曲線比較與當日訊號。法人資料來源：appr1ciat1/tw-institutional-stocker。">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{{font-family:system-ui,"Noto Sans TC",sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:28px 16px}}
 .wrap{{max-width:980px;margin:0 auto}}
 h1{{font-size:1.5rem;margin:0 0 4px}} .sub{{color:#94a3b8;margin:0 0 22px;font-size:.92rem}}
 h2{{font-size:1.1rem;margin:26px 0 10px}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:16px 18px;margin-bottom:18px}}
 table{{width:100%;border-collapse:collapse;font-size:.9rem}}
 th,td{{text-align:right;padding:7px 8px;border-bottom:1px solid #283449}} th{{color:#94a3b8;font-weight:600}}
 td:first-child,th:first-child{{text-align:left}}
 .disclaimer{{color:#64748b;font-size:.8rem;margin-top:18px;line-height:1.6}}
 a{{color:#60a5fa}}
</style></head><body><div class="wrap">
 <h1>📈 四策略績效比較</h1>
 <p class="sub">v8.5 / GUARD / SURGE / SURGE PRO 全期回測權益曲線（2019-01 → {today}，各自起點 normalize 為 100，log 軸）。資料更新：{today}。</p>

 <div class="card">
   <canvas id="eq" height="150"></canvas>
 </div>

 <h2>策略摘要（全期）</h2>
 <div class="card"><table>
   <tr><th>策略</th><th>年化</th><th>Sharpe</th><th>MDD</th><th>Calmar</th><th>交易</th><th>勝率</th></tr>
   {rows}
 </table></div>

 <h2>📋 當日交易計畫（SURGE PRO，最強策略）</h2>
 <div class="card">{sig_html}</div>

 <div class="disclaimer">
   ⚠️ <b>免責：</b>此為回測模擬績效，<b>非真實交易、非未來保證</b>。四策略共用同一組 v8.5 評分（Mom×3 + Trend×1），
   差別在事件引擎的去風險 / 分段強勢加碼參數（見 <a href="report_surge_pro.html">SURGE PRO 報表</a>）。<br>
   法人籌碼資料來源：<a href="https://github.com/appr1ciat1/tw-institutional-stocker">appr1ciat1/tw-institutional-stocker</a>。投資有風險，決策請自行負責。
 </div>
</div>
<script>
new Chart(document.getElementById('eq').getContext('2d'),{{
 type:'line',
 data:{{labels:{labels},datasets:{datasets_js}}},
 options:{{
   responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
   plugins:{{legend:{{labels:{{color:'#e2e8f0'}}}},title:{{display:false}}}},
   scales:{{
     x:{{ticks:{{color:'#64748b',maxTicksLimit:10}},grid:{{color:'#1e293b'}}}},
     y:{{type:'logarithmic',ticks:{{color:'#64748b'}},grid:{{color:'#1e293b'}},title:{{display:true,text:'權益(起點=100, log)',color:'#94a3b8'}}}}
   }}
 }}
}});
</script>
</body></html>"""


def main():
    results = run_all()
    sig_file, signals = today_signals()
    html = build_html(results, sig_file, signals)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已產出 {HTML_FILE}（四策略 + 折線圖 + 當日訊號，無 v9 內容）")


if __name__ == "__main__":
    main()
