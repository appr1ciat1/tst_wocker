#!/usr/bin/env python3
"""
twstk.report.compare — 三策略比較儀表板(每日可更新)

比較 v8.5 / v9 V3 / v9+反轉混合 的「歷史回測」與「Paper(自 4/22)」表現,
產出含時間軸折線圖 + 績效表的 HTML(strategy_compare.html)。

一次抓資料,三策略共用;EngineStrategy(v8.5/v9)與 WeightStrategy(反轉)
分別執行,v9+反轉混合 = 0.8×v9 + 0.2×反轉(daily-rebalanced 拆資金)。

用法:
    python -m twstk.report.compare                 # 預設 2022-01-01 起
    python -m twstk.report.compare --start 2019-01-01 --out strategy_compare.html
"""
import argparse
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

from strategies.registry import get_strategy
from strategies.base import MarketData, EngineStrategy, WeightStrategy, ExecConfig
from twstk.data import fetch_prices, liquid_universe, fetch_benchmark
from twstk.portfolio import PortfolioConfig, simulate_weights, equity_dataframe

PAPER_START = "2026-04-22"
CAPITAL = 200_000

try:
    from ai_report import EXTENDED_TICKERS as TICKERS
except Exception:  # noqa: BLE001
    TICKERS = ["2330", "2317", "2454", "2308", "2382", "2412", "2881", "2882",
               "2891", "3008", "2303", "1301", "1303", "2002"]


def _metrics(eq):
    eq = eq.dropna()
    if len(eq) < 5:
        return dict(ann=0, sharpe=0, mdd=0, calmar=0, total=0)
    dr = eq.pct_change().dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1
    yrs = len(eq) / 252
    ann = (1 + total) ** (1 / yrs) - 1 if yrs > 0 else 0
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() else 0
    mdd = ((eq - eq.cummax()) / eq.cummax()).min()
    return dict(ann=ann * 100, sharpe=sharpe, mdd=mdd * 100,
                calmar=(ann / abs(mdd) if mdd < 0 else 0), total=total * 100)


def build_data(start_date, end_date, universe_size=60):
    panel = fetch_prices(TICKERS, start_date=start_date, end_date=end_date)
    um = liquid_universe(panel.close, panel.volume, top_n=universe_size)
    mc = None
    try:
        mc = fetch_benchmark("0050", start_date=start_date, end_date=end_date)
    except Exception:  # noqa: BLE001
        pass
    return MarketData(close=panel.close, open=panel.open, high=panel.high,
                      low=panel.low, volume=panel.volume,
                      market_close=mc, universe_mask=um)


def strategy_run(name, data):
    """回傳 (equity Series, trades_df)。起始資金 CAPITAL。"""
    strat = get_strategy(name)
    exec_cfg = ExecConfig(initial_capital=CAPITAL)
    if isinstance(strat, EngineStrategy):
        td, ed = strat.run_engine(data, exec_cfg)
        return ed["Equity"], td
    if isinstance(strat, WeightStrategy):
        w = strat.target_weights(data)
        pcfg = PortfolioConfig(initial_capital=CAPITAL)
        st = simulate_weights(w, data.open, data.close, pcfg,
                              start=data.close.index[60].strftime("%Y-%m-%d"))
        import pandas as _pd
        return equity_dataframe(st)["Equity"], _pd.DataFrame(st.get("trades", []))
    raise TypeError(name)


def strategy_equity(name, data):
    return strategy_run(name, data)[0]


def blend_returns(eq_a, eq_b, wa, wb):
    """拆資金 daily-rebalanced:回傳混合 equity(起始 CAPITAL)。"""
    df = pd.DataFrame({"a": eq_a, "b": eq_b}).dropna()
    r = wa * df["a"].pct_change() + wb * df["b"].pct_change()
    return (1 + r.fillna(0)).cumprod() * CAPITAL


def normalize(eq, idx, start_value=CAPITAL):
    """對齊到 idx、從首個有效點重設為 start_value。"""
    eq = eq.reindex(idx).ffill().dropna()
    if len(eq) == 0:
        return eq
    return eq / eq.iloc[0] * start_value


def panel(curves, idx):
    """回傳 {name: normalized equity (list)}, dates(list), metrics{name:...}"""
    out, mets = {}, {}
    for name, eq in curves.items():
        ne = normalize(eq, idx)
        out[name] = [round(float(x), 0) for x in ne.reindex(idx).ffill().values]
        mets[name] = _metrics(ne)
    return out, mets


def generate(start_date, out_path):
    end_date = date.today().isoformat()
    print(f"📊 比較儀表板：{start_date} → {end_date}")
    data = build_data(start_date, end_date)

    eq85 = strategy_equity("momentum_v85", data)
    eq9, tr9 = strategy_run("hybrid_tiered_v9", data)   # v9 = production paper
    eqrev = strategy_equity("reversal_20d", data)
    eqblend = blend_returns(eq9, eqrev.reindex(eq9.index).ffill(), 0.8, 0.2)

    curves = {"v8.5": eq85, "v9 V3": eq9, "v9+反轉混合": eqblend}

    # 共同日期軸
    common = eq9.index
    bt_dates = [d.strftime("%Y-%m-%d") for d in common]
    bt_curves, bt_mets = panel(curves, common)

    # Paper:自 4/22 起,各自重設為 CAPITAL
    paper_idx = common[common >= pd.Timestamp(PAPER_START)]
    paper_dates = [d.strftime("%Y-%m-%d") for d in paper_idx]
    paper_curves, paper_mets = panel(curves, paper_idx)

    html = _html(bt_dates, bt_curves, bt_mets, paper_dates, paper_curves, paper_mets,
                 start_date, end_date)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已輸出 {out_path}")

    # 同時輸出正確的 paper_trading.html(production = v9，自 4/22 真實引擎損益，
    # 取代壞掉、會凍結的 legacy paper_tracker 版本)
    p_eq = normalize(eq9, paper_idx)
    p_dates = paper_dates
    p_vals = [round(float(x), 0) for x in p_eq.reindex(paper_idx).ffill().values]
    p_m = _metrics(p_eq)
    paper_html = _paper_html(p_dates, p_vals, p_m, tr9, end_date)
    with open("paper_trading.html", "w", encoding="utf-8") as f:
        f.write(paper_html)
    print("✅ 已輸出 paper_trading.html（v9 production，自 4/22）")
    # 主控台摘要
    for title, mets in [("回測", bt_mets), ("Paper(自4/22)", paper_mets)]:
        print(f"\n[{title}]")
        for n, m in mets.items():
            print(f"  {n:<14} 年化 {m['ann']:+6.1f}%  Sharpe {m['sharpe']:.2f}  "
                  f"MDD {m['mdd']:+6.1f}%  Calmar {m['calmar']:.2f}")


_COLORS = {"v8.5": "#888888", "v9 V3": "#00c2ff", "v9+反轉混合": "#00ff88"}


def _table(mets, paper=False):
    rows = ""
    for n, m in mets.items():
        c = f"color:{_COLORS[n]};font-weight:bold"
        if paper:  # 短期間不顯示年化/Calmar(會誤導)
            rows += (f"<tr><td style='{c}'>{n}</td>"
                     f"<td>{m['total']:+.1f}%</td><td>{m['sharpe']:.2f}</td>"
                     f"<td>{m['mdd']:.1f}%</td></tr>")
        else:
            rows += (f"<tr><td style='{c}'>{n}</td>"
                     f"<td>{m['total']:+.1f}%</td><td>{m['ann']:+.1f}%</td>"
                     f"<td>{m['sharpe']:.2f}</td><td>{m['mdd']:.1f}%</td>"
                     f"<td>{m['calmar']:.2f}</td></tr>")
    return rows


def _datasets(curves):
    ds = []
    for n, vals in curves.items():
        ds.append("{label:%s,data:%s,borderColor:'%s',backgroundColor:'%s',"
                  "borderWidth:2,pointRadius:0,tension:0.1}"
                  % (json.dumps(n, ensure_ascii=False), json.dumps(vals),
                     _COLORS[n], _COLORS[n]))
    return "[" + ",".join(ds) + "]"


def _html(btd, btc, btm, ppd, ppc, ppm, start, end):
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略比較儀表板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;margin:0;padding:20px}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:32px}}
.sub{{color:#8b949e;font-size:.85rem}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin:14px 0}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{padding:6px 10px;text-align:right;border-bottom:1px solid #21262d}}
th:first-child,td:first-child{{text-align:left}}
th{{color:#8b949e;font-weight:600}}
canvas{{max-height:340px}}
</style></head><body>
<h1>📊 策略比較儀表板</h1>
<div class="sub">回測區間 {start} → {end}｜Paper 自 {PAPER_START} 起｜初始資金 {CAPITAL:,}｜每日更新</div>

<div class="card"><h2>歷史回測 — 權益曲線</h2><canvas id="btChart"></canvas></div>
<div class="card"><h2>歷史回測 — 績效</h2>
<table><thead><tr><th>策略</th><th>總報酬</th><th>年化</th><th>Sharpe</th><th>MDD</th><th>Calmar</th></tr></thead>
<tbody>{_table(btm)}</tbody></table></div>

<div class="card"><h2>Paper Trading（自 {PAPER_START}）— 權益曲線</h2><canvas id="ppChart"></canvas></div>
<div class="card"><h2>Paper Trading — 績效</h2>
<table><thead><tr><th>策略</th><th>總報酬</th><th>Sharpe</th><th>MDD</th></tr></thead>
<tbody>{_table(ppm, paper=True)}</tbody></table>
<div class="sub" style="margin-top:8px">⚠️ Paper 期間僅約 2 個月,年化 / Calmar 會嚴重失真故不顯示;以總報酬與 MDD 為準。</div></div>

<div class="sub">v9+反轉混合 = 80% v9 V3 + 20% 反轉(均值回歸)拆資金,daily-rebalanced。
本頁僅供研究,非投資建議。</div>
<script>
const opt=(t)=>({{responsive:true,interaction:{{mode:'index',intersect:false}},
plugins:{{legend:{{labels:{{color:'#e6edf3'}}}},title:{{display:false}}}},
scales:{{x:{{ticks:{{color:'#8b949e',maxTicksLimit:10}},grid:{{color:'#21262d'}}}},
y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}}}});
new Chart(document.getElementById('btChart'),{{type:'line',
data:{{labels:{json.dumps(btd)},datasets:{_datasets(btc)}}},options:opt()}});
new Chart(document.getElementById('ppChart'),{{type:'line',
data:{{labels:{json.dumps(ppd)},datasets:{_datasets(ppc)}}},options:opt()}});
</script></body></html>"""


def _paper_html(dates, vals, m, trades_df, end_date):
    """正確的 paper_trading.html：production = v9，自 4/22 真實引擎損益。"""
    rows = ""
    ntr = 0
    try:
        if trades_df is not None and len(trades_df) and "Exit_Date" in trades_df.columns:
            t = trades_df[trades_df["Exit_Date"] >= PAPER_START].sort_values("Exit_Date")
            ntr = len(t)
            for _, r in t.tail(25).iloc[::-1].iterrows():
                ret = float(r.get("Return_Pct", 0)) * 100
                col = "#00ff88" if ret >= 0 else "#ff5555"
                rows += (f"<tr><td>{r.get('Ticker')}</td><td>{r.get('Entry_Date')}</td>"
                         f"<td>{r.get('Exit_Date')}</td><td>{r.get('Entry_Price')}</td>"
                         f"<td>{r.get('Exit_Price')}</td>"
                         f"<td style='color:{col}'>{ret:+.1f}%</td>"
                         f"<td>{r.get('Reason','')}</td></tr>")
    except Exception:
        pass
    cur = vals[-1] if vals else CAPITAL
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading — v9 production</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;margin:0;padding:20px}}
h1{{font-size:1.4rem}} .sub{{color:#8b949e;font-size:.85rem}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin:14px 0}}
.k{{display:inline-block;margin-right:28px}} .k b{{font-size:1.5rem}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}}
th,td{{padding:6px 10px;text-align:right;border-bottom:1px solid #21262d}}
th:first-child,td:first-child{{text-align:left}} th{{color:#8b949e}}
canvas{{max-height:360px}}
</style></head><body>
<h1>📒 Paper Trading — v9 production（自 {PAPER_START}）</h1>
<div class="sub">資料更新至 {end_date}｜初始資金 {CAPITAL:,}｜每交易日自動更新</div>
<div class="card">
  <span class="k">總報酬 <b style="color:{'#00ff88' if m['total']>=0 else '#ff5555'}">{m['total']:+.1f}%</b></span>
  <span class="k">目前權益 <b>{cur:,.0f}</b></span>
  <span class="k">最大回撤 <b>{m['mdd']:.1f}%</b></span>
  <span class="k">Sharpe <b>{m['sharpe']:.2f}</b></span>
  <span class="k">平倉筆數 <b>{ntr}</b></span>
</div>
<div class="card"><canvas id="eq"></canvas></div>
<div class="card"><h3 style="margin-top:0">近期平倉(最多 25 筆)</h3>
<table><thead><tr><th>代號</th><th>進場日</th><th>出場日</th><th>進</th><th>出</th><th>報酬</th><th>原因</th></tr></thead>
<tbody>{rows or '<tr><td colspan=7 style="text-align:center;color:#8b949e">尚無平倉交易</td></tr>'}</tbody></table></div>
<div class="sub">本頁為 v9（Hybrid Tiered）自 4/22 的真實引擎模擬損益,取代舊版會凍結的 paper_tracker。
僅供研究,非投資建議。</div>
<script>
new Chart(document.getElementById('eq'),{{type:'line',
data:{{labels:{json.dumps(dates)},datasets:[{{label:'v9 權益',data:{json.dumps(vals)},
borderColor:'#00c2ff',borderWidth:2,pointRadius:0,tension:0.1}}]}},
options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#e6edf3'}}}}}},
scales:{{x:{{ticks:{{color:'#8b949e',maxTicksLimit:10}},grid:{{color:'#21262d'}}}},
y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}}}}}});
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01", help="回測起始日")
    ap.add_argument("--out", default="strategy_compare.html", help="輸出 HTML")
    args = ap.parse_args(argv)
    generate(args.start, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
