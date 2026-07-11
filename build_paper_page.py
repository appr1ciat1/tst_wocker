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

from inst_widget import build_inst_widget
from strategies.registry import get_strategy
from strategies.base import ExecConfig
from twstk.backtest.engine import RunConfig, build_market_data
from twstk.backtest.metrics import compute_risk_metrics

CAPITAL = 1_000_000

# (顯示名, 註冊名, 顏色, 一句說明)
# 顏色採高對比、相互區隔的色相（紅/琥珀/綠/藍），四策略一目了然。
STRATS = [
    ("SURGE PRO", "mom_surge_pro", "#ef4444", "去風險 + 更激進分段加碼，報酬最高"),
    ("SURGE",     "mom_surge",     "#f59e0b", "去風險 + 分段強勢加碼"),
    ("GUARD",     "mom_guard",     "#10b981", "弱勢去風險，不加碼，最穩健"),
    ("v8.5",      "momentum_v85",  "#3b82f6", "純動量基準（優化前）"),
]

# 每個追蹤策略各產一個 paper 頁。orders 檔：SURGE PRO 走 ai_report 預設 orders_<date>.json；
# GUARD 由 workflow 在 GUARD 那步 cp 成 orders_guard_latest.json（否則會被 SURGE PRO 覆蓋）。
TRACKS = [
    {"disp": "SURGE PRO", "reg": "mom_surge_pro", "color": "#ef4444", "role": "追最高報酬",
     "file": "paper_trading.html", "orders": "artifacts/orders_2*.json", "report": "report_surge_pro.html"},
    {"disp": "GUARD", "reg": "mom_guard", "color": "#10b981", "role": "最穩健·相關性分散",
     "file": "paper_trading_guard.html", "orders": "artifacts/orders_guard_*.json", "report": "report_guard.html"},
]


# ── 股票代碼 → 中文名 / 市場（給 Yahoo Finance 連結）─────────────
_STOCK_META = None


def _stock_meta():
    """{bare_code: (中文名, 市場)}；來源＝法人快照 code/name/market（快取一次）。"""
    global _STOCK_META
    if _STOCK_META is None:
        _STOCK_META = {}
        try:
            from twstk.data.institutional import fetch_stock_three_inst_latest
            for x in (fetch_stock_three_inst_latest() or []):
                _STOCK_META[str(x.get("code"))] = (x.get("name", ""), str(x.get("market", "")))
        except Exception:
            _STOCK_META = {}
    return _STOCK_META


def _stock_link(ticker):
    """回傳 HTML <a>：『代碼 中文名』，點擊跳 Yahoo Finance（上市.TW / 上櫃.TWO）。"""
    if ticker is None or str(ticker).strip() in ("", "None", "-"):
        return "-"
    raw = str(ticker)
    code = raw.split(".")[0]
    name, market = _stock_meta().get(code, ("", ""))
    if raw.endswith(".TWO"):
        suf = ".TWO"
    elif raw.endswith(".TW"):
        suf = ".TW"
    else:
        suf = ".TWO" if market.upper() in ("TPEX", "OTC", "TWO", "櫃買", "上櫃") else ".TW"
    url = f"https://tw.stock.yahoo.com/quote/{code}{suf}"
    label = f"{code}{('&nbsp;' + name) if name else ''}"
    return f"<a href='{url}' target='_blank' rel='noopener'>{label}</a>"


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
    eq_map = {}
    trades_map = {}
    for disp, reg, color, desc in STRATS:
        print(f"▶ 回測 {disp} ({reg}) ...")
        strat = get_strategy(reg)
        trades, equity = strat.run_engine(data, exec_cfg)
        m = compute_risk_metrics(equity, trades, CAPITAL)
        eq = (equity["Equity"] if "Equity" in equity.columns else equity.iloc[:, 0]).sort_index()
        eq = eq.dropna()
        eq_map[disp] = eq
        trades_map[disp] = trades
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
    return out, data, eq_map, trades_map


# 近 90 天權益曲線比較用的 ETF（台股 ETF；上市走 .TW，上櫃走 .TWO，下方自動後援）
# ETF 用紫/粉色相 + 虛線，與四策略（紅/琥珀/綠/藍實線）明顯區隔。
ETF_BENCH = [
    ("00877", "復華中國5G", "#a855f7"),
    ("00935", "野村臺灣新科技50", "#ec4899"),
]


def _fetch_etf_close(code, start_date, end_date):
    """抓 ETF 買進持有淨值（起點=1）。先試 .TW（上市），無資料再試 .TWO（上櫃，如 00877）。"""
    try:
        from twstk.data import fetch_benchmark
        s = fetch_benchmark(code, start_date=start_date, end_date=end_date)
        if s is not None and len(s) >= 5:
            return s
    except Exception:
        pass
    try:
        import numpy as np
        import yfinance as yf
        df = yf.download(f"{code}.TWO", start=start_date, end=end_date, progress=False)
        if df is None or df.empty:
            return None
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = pd.to_numeric(close, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(close) < 5:
            return None
        return close / close.iloc[0]
    except Exception:
        return None


def ninety_day_curves(eq_map, n_days=90):
    """近 n_days 天：4 策略 + 3 ETF 的權益曲線（各自起點 normalize 為 100）。

    策略走自身回測權益；ETF 走 fetch_benchmark(買進持有)。全部對齊到策略交易日 index、
    截取最近 n_days 天、以視窗首日為 100 重新基準化，方便同圖比較。
    """
    if not eq_map:
        return None
    # 參考交易日 index：用最長的策略權益（四策略同 index，取任一）
    ref = max(eq_map.values(), key=len).sort_index()
    last = ref.index[-1]
    start = last - pd.Timedelta(days=n_days)
    win_idx = ref.index[ref.index >= start]
    if len(win_idx) < 5:
        return None

    series = {}  # label -> (values list aligned to win_idx, color, is_etf_dashed)
    # 策略（實線）
    for disp, _reg, color, _desc in STRATS:
        eq = eq_map.get(disp)
        if eq is None:
            continue
        s = eq.reindex(win_idx).ffill().bfill()
        if s.isna().all() or float(s.iloc[0]) == 0:
            continue
        series[disp] = ([round(float(v) / float(s.iloc[0]) * 100.0, 2) for v in s.values], color, False)
    # ETF（buy-and-hold；上市 .TW / 上櫃 .TWO 自動後援）
    bstart = (start - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    bend = (last + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    for code, name, color in ETF_BENCH:
        try:
            bench = _fetch_etf_close(code, bstart, bend)
            if bench is None or len(bench) < 5:
                print(f"   ⚠️ ETF {code} 無資料，跳過")
                continue
            bench.index = pd.to_datetime(bench.index)
            s = bench.reindex(win_idx).ffill().bfill()
            if s.isna().all() or float(s.iloc[0]) == 0:
                continue
            label = f"{code} {name}"
            series[label] = ([round(float(v) / float(s.iloc[0]) * 100.0, 2) for v in s.values], color, True)
        except Exception as e:
            print(f"   ⚠️ ETF {code} 抓取失敗: {e}")
    if not series:
        return None
    labels = [d.strftime("%Y-%m-%d") for d in win_idx]
    datasets = [{"label": lab, "data": vals, "color": col, "dash": dash}
                for lab, (vals, col, dash) in series.items()]
    return {"labels": labels, "datasets": datasets,
            "start": labels[0], "end": labels[-1], "n": len(labels)}


def recent_buy_signal_rounds(data, top_k=7, threshold=2.0, n_rounds=3, round_len=10):
    """近 n_rounds×round_len 個台股交易日的「歷史買進訊號」，每輪列出買進訊號 ≥2 次的標的。

    買進訊號＝策略每日「想買進的 Top-K 標的」：score≥threshold 且 close>ma_long 且在
    流動性池內、且當日大盤 regime 為多頭（0050>60MA，策略在弱勢 regime 不進場）才計。
    計的是「被選為買進候選的天數」（含當時已持有續抱者），反映持續看好度。
    每 round_len 個交易日為一輪，回傳 n_rounds 輪（最新一輪在前）。
    """
    bundle = get_strategy("momentum_v85").prepare(data)
    score = bundle.total_score
    eligible = score >= threshold
    if bundle.ma_long is not None:
        eligible = eligible & (data.close.reindex_like(score) > bundle.ma_long)
    if data.universe_mask is not None:
        eligible = eligible & data.universe_mask.reindex_like(score).fillna(False)
    masked = score.where(eligible)
    # method='first'：同分時依欄位序打破平手，確保「恰好」取 Top-K（避免 average 讓第 K+1 檔也 <=K）
    ranks = masked.rank(axis=1, method="first", ascending=False)
    selected = (ranks <= top_k) & masked.notna()

    # regime 閘：大盤 0050 <= 60MA 的弱勢日，策略不進場 → 該日不計任何買進訊號
    try:
        if data.market_close is not None:
            mc = data.market_close.reindex(selected.index).ffill()
            ma60 = mc.rolling(60, min_periods=20).mean()
            regime_on = (mc > ma60).fillna(True)   # 早期資料不足 → 預設多頭
            selected = selected.mul(regime_on.astype(int), axis=0).astype(bool)
    except Exception:
        pass

    dates = list(selected.index)
    if len(dates) < round_len:
        return []
    need = n_rounds * round_len
    tail_dates = dates[-need:] if len(dates) >= need else dates

    # 代號→股名（best-effort，用法人快照的 code/name；失敗就只顯示代號）
    names = {}
    try:
        from twstk.data.institutional import fetch_stock_three_inst_latest
        for x in (fetch_stock_three_inst_latest() or []):
            names[str(x.get("code"))] = x.get("name", "")
    except Exception:
        names = {}

    def _name(code):
        c = str(code).split(".")[0]
        return names.get(c, "")

    rounds = []
    for ri in range(n_rounds):
        end_i = len(tail_dates) - ri * round_len
        start_i = end_i - round_len
        if start_i < 0:
            break
        rdates = tail_dates[start_i:end_i]
        if not rdates:
            break
        sub = selected.loc[rdates]
        counts = sub.sum(axis=0)
        counts = counts[counts >= 2].sort_values(ascending=False)
        stocks = [(str(c).split(".")[0], _name(c), int(counts[c])) for c in counts.index][:30]
        rounds.append({
            "idx": ri + 1,
            "start": str(pd.Timestamp(rdates[0]).date()),
            "end": str(pd.Timestamp(rdates[-1]).date()),
            "n_days": len(rdates),
            "stocks": stocks,
        })
    return rounds


def two_month(spro_eq, spro_trades):
    """SURGE PRO 過去兩個月（最後 ~60 日曆天）的指標 + 交易紀錄。"""
    if spro_eq is None or len(spro_eq) < 5:
        return {}, []
    last = spro_eq.index[-1]
    start = last - pd.Timedelta(days=61)
    eq2 = spro_eq[spro_eq.index >= start]
    if len(eq2) < 5:
        eq2 = spro_eq.tail(42)
    # 交易：以該區間「出場日」計
    rows = []
    if spro_trades is not None and not spro_trades.empty and "Exit_Date" in spro_trades.columns:
        td = spro_trades.copy()
        td["_ex"] = pd.to_datetime(td["Exit_Date"], errors="coerce")
        td = td[td["_ex"] >= start].sort_values("_ex", ascending=False)
        for _, r in td.iterrows():
            rows.append({
                "ticker": r.get("Ticker"), "entry_d": str(r.get("Entry_Date")),
                "exit_d": str(r.get("Exit_Date")), "entry_p": r.get("Entry_Price"),
                "exit_p": r.get("Exit_Price"), "ret": float(r.get("Return_Pct", 0)),
                "reason": str(r.get("Reason", "")), "days": int(r.get("Days_Held", 0)),
            })
    # 指標（以該區間權益）
    rets = eq2.pct_change().dropna()
    n = len(eq2)
    total_ret = float(eq2.iloc[-1] / eq2.iloc[0] - 1) if n else 0.0
    ann_vol = float(rets.std() * (252 ** 0.5)) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / rets.std() * (252 ** 0.5)) if rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    dvol = float(downside.std() * (252 ** 0.5)) if len(downside) > 1 else 0.0
    # 年化報酬（供 Sortino/Calmar）
    yrs = n / 252 if n else 1
    ann_ret = (1 + total_ret) ** (1 / yrs) - 1 if yrs > 0 and (1 + total_ret) > 0 else total_ret
    sortino = float(ann_ret / dvol) if dvol > 0 else 0.0
    cummax = eq2.cummax()
    mdd = float((eq2 / cummax - 1).min())
    calmar = float(ann_ret / abs(mdd)) if mdd != 0 else 0.0
    wins = sum(1 for r in rows if r["ret"] > 0)
    stats = {
        "start": str(eq2.index[0].date()), "end": str(eq2.index[-1].date()),
        "total_ret": total_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "sortino": sortino, "mdd": mdd, "calmar": calmar,
        "n_trades": len(rows), "win_rate": (wins / len(rows)) if rows else 0.0,
    }
    return stats, rows


def recent_sells(spro_trades, n_days=7):
    """SURGE PRO 近 n_days 的出場（賣出訊號）。"""
    if spro_trades is None or spro_trades.empty or "Exit_Date" not in spro_trades.columns:
        return []
    td = spro_trades.copy()
    td["_ex"] = pd.to_datetime(td["Exit_Date"], errors="coerce")
    last = td["_ex"].max()
    if pd.isna(last):
        return []
    td = td[td["_ex"] >= last - pd.Timedelta(days=n_days)].sort_values("_ex", ascending=False)
    return [{
        "ticker": r.get("Ticker"), "exit_d": str(r.get("Exit_Date")),
        "exit_p": r.get("Exit_Price"), "ret": float(r.get("Return_Pct", 0)),
        "reason": str(r.get("Reason", "")),
    } for _, r in td.iterrows()]


def today_signals(orders_glob="artifacts/orders_2*.json"):
    """讀最新符合 orders_glob 的 orders 檔 → 今日買入訊號。
    SURGE PRO 走 orders_2*.json（ai_report 預設，日期命名）；GUARD 走 orders_guard_*.json。"""
    files = sorted(glob.glob(orders_glob), key=lambda f: os.path.getmtime(f))
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


def build_html(results, sig_file, signals, sells, tm_stats, tm_trades, buy_rounds=None, ninety=None, track=None):
    today = date.today().strftime("%Y-%m-%d")
    track = track or TRACKS[0]
    t_disp, t_color = track["disp"], track["color"]
    t_role, t_report = track.get("role", ""), track.get("report", "report_surge_pro.html")
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
    # 買進訊號（股票欄＝代碼+中文名，點擊跳 Yahoo Finance）
    if signals:
        buy_rows = "".join(
            f"<tr><td>{_stock_link(s['ticker'])}</td><td>{s['entry']}</td><td>{s['tp']}</td>"
            f"<td>{s['sl']}</td><td>{s['exec'] or '-'}</td></tr>" for s in signals[:20]
        )
        buy_html = ("<table><tr><th>股票</th><th>參考進場</th><th>停利</th><th>停損</th><th>執行日</th></tr>"
                    f"{buy_rows}</table>")
    else:
        buy_html = "<p style='color:#94a3b8'>今日無新買進訊號。</p>"
    # 賣出訊號（近 7 日出場）
    if sells:
        sell_rows = "".join(
            f"<tr><td>{_stock_link(s['ticker'])}</td><td>{s['exit_d']}</td><td>{s['exit_p']}</td>"
            f"<td style='color:{'#4ade80' if s['ret']>0 else '#f87171'}'>{s['ret']*100:+.1f}%</td>"
            f"<td>{s['reason']}</td></tr>" for s in sells[:20]
        )
        sell_html = ("<table><tr><th>股票</th><th>出場日</th><th>出場價</th><th>損益</th><th>原因</th></tr>"
                     f"{sell_rows}</table>")
    else:
        sell_html = "<p style='color:#94a3b8'>近 7 日無出場。</p>"
    sig_html = (
        f"<p style='color:#94a3b8'>買進來源：{os.path.basename(sig_file or '（無）')}（{t_disp} 次一交易日進場計畫）。"
        "賣出＝近 7 日 TP/SL/時間到期出場。股票可點擊跳 Yahoo Finance。</p>"
        f"<h3 style='font-size:.98rem;margin:6px 0 4px;color:{t_color}'>🟢 買進訊號</h3>{buy_html}"
        f"<h3 style='font-size:.98rem;margin:14px 0 4px;color:#93c5fd'>🔴 賣出訊號</h3>{sell_html}"
    )
    # SURGE PRO 過去兩個月
    if tm_stats:
        def _m(label, val, good_high=True):
            return f"<div class='kpi'><div class='kl'>{label}</div><div class='kv'>{val}</div></div>"
        kpis = (
            _m("報酬率", f"{tm_stats['total_ret']*100:+.1f}%")
            + _m("波動率(年化)", f"{tm_stats['ann_vol']*100:.1f}%")
            + _m("Sharpe", f"{tm_stats['sharpe']:.2f}")
            + _m("Sortino", f"{tm_stats['sortino']:.2f}")
            + _m("最大回撤", f"{tm_stats['mdd']*100:.1f}%")
            + _m("Calmar", f"{tm_stats['calmar']:.2f}")
            + _m("交易數", f"{tm_stats['n_trades']}")
            + _m("勝率", f"{tm_stats['win_rate']*100:.0f}%")
        )
        if tm_trades:
            tr_rows = "".join(
                f"<tr><td>{_stock_link(t['ticker'])}</td><td>{t['entry_d']}</td><td>{t['exit_d']}</td>"
                f"<td>{t['entry_p']}</td><td>{t['exit_p']}</td>"
                f"<td style='color:{'#4ade80' if t['ret']>0 else '#f87171'}'>{t['ret']*100:+.1f}%</td>"
                f"<td>{t['reason']}</td><td>{t['days']}</td></tr>" for t in tm_trades[:60]
            )
            tr_table = ("<table><tr><th>股票</th><th>進場日</th><th>出場日</th><th>進場價</th><th>出場價</th>"
                        f"<th>損益</th><th>原因</th><th>持有</th></tr>{tr_rows}</table>")
        else:
            tr_table = "<p style='color:#94a3b8'>此區間無已完成交易。</p>"
        tm_html = (
            f"<p style='color:#94a3b8'>區間 {tm_stats['start']} → {tm_stats['end']}（約兩個月，{tm_stats['n_trades']} 筆已完成交易）</p>"
            f"<div class='kpis'>{kpis}</div>{tr_table}"
        )
    else:
        tm_html = "<p style='color:#94a3b8'>資料不足。</p>"

    # 近 30 日歷史買進訊號（3 輪 × 10 交易日，每輪 ≥2 次）
    if buy_rounds:
        round_blocks = ""
        for rd in buy_rounds:
            tag = "（最新）" if rd["idx"] == 1 else ""
            if rd["stocks"]:
                srows = "".join(
                    f"<tr><td>{_stock_link(code)}</td>"
                    f"<td><b style='color:{t_color}'>{cnt}</b> / {rd['n_days']} 日</td></tr>"
                    for code, nm, cnt in rd["stocks"]
                )
                stbl = ("<table><tr><th>股票</th><th>買進訊號次數</th></tr>"
                        f"{srows}</table>")
            else:
                stbl = "<p style='color:#94a3b8'>本輪無出現 ≥2 次買進訊號的標的。</p>"
            round_blocks += (
                f"<h3 style='font-size:.98rem;margin:14px 0 4px;color:#fcd34d'>"
                f"第 {rd['idx']} 輪{tag} · {rd['start']} → {rd['end']}</h3>{stbl}"
            )
        rounds_html = (
            "<table class='note'><tbody>"
            "<tr><td class='nk'>買進訊號</td><td>策略每日「想買進的 Top-7 標的」——"
            "v8.5 動量評分 ≥2.0、站上 60MA、在流動性池內。</td></tr>"
            "<tr><td class='nk'>Regime 閘</td><td>僅當日大盤多頭（<b>0050 &gt; 60MA</b>）才計；"
            "弱勢日策略不進場，不列訊號。</td></tr>"
            "<tr><td class='nk'>計數方式</td><td>該股被選為當日買進候選的<b>天數</b>"
            "（<b>含當時已持有續抱者</b>）→ 反映「持續看好度」。</td></tr>"
            "<tr><td class='nk'>分輪 / 排序</td><td>每 <b>10 個台股交易日</b>為一輪、共 3 輪，最新一輪在前；"
            "每輪只列 <b>≥2 次</b>的標的，依次數由多到少。</td></tr>"
            "<tr><td class='nk'>⚠️ 與上方差異</td><td>「今日買賣訊號」是<b>新買進</b>（已排除持倉），"
            "故當日新入選的股票在此僅 1 天、可能不在 ≥2 名單——兩者衡量不同，非矛盾。</td></tr>"
            "</tbody></table>"
            f"{round_blocks}"
        )
    else:
        rounds_html = "<p style='color:#94a3b8'>資料不足，無法計算歷史買進訊號。</p>"

    # 近 90 天權益曲線（4 策略 + 3 ETF）
    if ninety and ninety.get("datasets"):
        n90_labels = json.dumps(ninety["labels"], ensure_ascii=False)
        _ds = []
        for d in ninety["datasets"]:
            dash = ",borderDash:[6,4]" if d.get("dash") else ""
            _ds.append(
                "{label:%s,data:%s,borderColor:'%s',backgroundColor:'transparent',"
                "fill:false,tension:0.2,pointRadius:0,borderWidth:2%s}"
                % (json.dumps(d["label"], ensure_ascii=False), json.dumps(d["data"]), d["color"], dash)
            )
        n90_ds_js = "[" + ",".join(_ds) + "]"
        n90_note = (
            f"近 {ninety['n']} 個交易日（{ninety['start']} → {ninety['end']}）："
            "4 策略回測權益（實線）vs ETF 買進持有（虛線），各自起點＝100（線性軸）。"
            "<br>⚠️ 此窗為單向強漲段（大盤 0050 約 +40%）：不去風險的 v8.5 滿倉故短線報酬最高、"
            "SURGE PRO 去風險最多（動態減碼）故短線最低——但 <b>全期年化反而是 SURGE PRO 最高</b>"
            "（見上方摘要表），去風險的價值要在崩盤年才顯現。<b>短窗排序不代表長期優劣。</b>"
        )
    else:
        n90_labels = "[]"
        n90_ds_js = "[]"
        n90_note = "資料不足，無法繪製近 90 天權益曲線。"

    # 選哪個策略？（依市場情境）——靜態對照表，與 index 首頁一致（用 paper 圖配色）
    guide_html = (
        "<p style=\"color:#94a3b8;margin:0 0 8px\">四策略<b>選股訊號與弱勢去風險邏輯完全相同</b>，"
        "差別只在強勢時加碼的積極度與進場挑剔度。</p>"
        "<table><tr><th>市場情境</th><th>最適策略</th><th>原因</th></tr>"
        "<tr><td>🚀 強勢延續多頭、VIX 低、龍頭領漲</td><td><b style='color:#ef4444'>SURGE PRO</b></td>"
        "<td>激進分段加碼，最大化報酬</td></tr>"
        "<tr><td>📈 漲跌互現、長期向上的波段</td><td><b style='color:#f59e0b'>SURGE</b></td>"
        "<td>去風險＋適度加碼，最佳平衡</td></tr>"
        "<tr><td>🌊 廣泛齊漲（雨露均霑）</td><td><b style='color:#10b981'>GUARD</b> / <b style='color:#3b82f6'>v8.5</b></td>"
        "<td>分散／滿倉廣泛參與勝過集中</td></tr>"
        "<tr><td>〽️ 震盪盤整、方向不明</td><td><b style='color:#10b981'>GUARD</b> / <b style='color:#f59e0b'>SURGE</b></td>"
        "<td>graduated 弱勢自動減碼保護</td></tr>"
        "<tr><td>💥 升息／系統性崩盤（如 2022）</td><td><b style='color:#f59e0b'>SURGE</b>（最防守）</td>"
        "<td>去風險＋不過度集中，崩盤年 OOS 最佳</td></tr>"
        "</table>"
        "<p style='color:#cbd5e1;font-size:.88rem;margin:12px 0 0;line-height:1.65'>"
        "全期 2019–2026 數字 <b style='color:#ef4444'>SURGE PRO</b> 最強（年化／Sharpe／Calmar／PBO 皆居首）；"
        "全天候平衡 <b style='color:#f59e0b'>SURGE</b> 最佳（回撤最淺 −21.5%、崩盤抗跌最好）。"
        "<b>要榨乾回測優勢且能扛崩盤 → SURGE PRO；務實怕崩盤 → SURGE。</b></p>"
    )

    inst_widget_html = build_inst_widget()

    return f"""<!DOCTYPE html>
<html lang="zh-TW"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Trading · {t_disp} — {today}</title>
<meta name="description" content="{t_disp} 策略 paper trading：當日買賣訊號、過去兩個月績效、四策略比較。法人資料來源：appr1ciat1/tw-institutional-stocker。">
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
 .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));gap:10px;margin:6px 0 14px}}
 .kpi{{background:#0f1b2e;border:1px solid #283449;border-radius:10px;padding:9px 11px}}
 .kl{{color:#94a3b8;font-size:.72rem}} .kv{{font-size:1.12rem;font-weight:700;margin-top:2px}}
 table.note{{margin:0 0 12px;font-size:.85rem;background:#0f1b2e;border:1px solid #283449;border-radius:10px;overflow:hidden}}
 table.note td{{text-align:left;color:#cbd5e1;padding:8px 12px;border-bottom:1px solid #1e293b;line-height:1.5}}
 table.note tr:last-child td{{border-bottom:none}}
 table.note td.nk{{color:#93c5fd;font-weight:700;white-space:nowrap;width:1%;vertical-align:top;background:#132033}}
</style></head><body><div class="wrap">
 <h1>📈 Paper Trading · <span style="color:{t_color}">{t_disp}</span> <span style="font-size:1rem;color:#94a3b8">（{t_role}）</span></h1>
 <p class="sub">追蹤 <b style="color:{t_color}">{t_disp}</b> 策略的當日買賣訊號與近兩個月績效，附四策略全期權益曲線比較。每個台股交易日收盤後自動更新。資料：{today}。
 <br>另有：<a href="paper_trading.html">SURGE PRO paper</a> · <a href="paper_trading_guard.html">GUARD paper</a> · <a href="index.html">策略選單</a></p>

 <div class="card">
   <canvas id="eq" height="150"></canvas>
 </div>

 <h2>策略摘要（全期）</h2>
 <div class="card"><table>
   <tr><th>策略</th><th>年化</th><th>Sharpe</th><th>MDD</th><th>Calmar</th><th>交易</th><th>勝率</th></tr>
   {rows}
 </table></div>

 <h2>📊 近 90 天權益曲線（4 策略 vs 3 ETF）</h2>
 <div class="card">
   <p style="color:#94a3b8;margin:0 0 8px">{n90_note}</p>
   <canvas id="eq90" height="150"></canvas>
 </div>

 <h2>📌 選哪個策略？（依市場情境）</h2>
 <div class="card">{guide_html}</div>

 <h2>📋 今日買賣訊號（{t_disp}）</h2>
 <div class="card">{sig_html}</div>

 {inst_widget_html}

 <h2>🔁 近 30 日歷史買進訊號（3 輪 × 10 交易日，每輪 ≥2 次）</h2>
 <div class="card">{rounds_html}</div>

 <h2>🗓️ {t_disp} 過去兩個月</h2>
 <div class="card">{tm_html}</div>

 <div class="disclaimer">
   ⚠️ <b>免責：</b>此為回測模擬績效，<b>非真實交易、非未來保證</b>。四策略共用同一組 v8.5 評分（Mom×3 + Trend×1），
   差別在事件引擎的去風險 / 分段強勢加碼參數（見 <a href="{t_report}">{t_disp} 報表</a>）。<br>
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
var _n90ds={n90_ds_js};
if(_n90ds.length){{
new Chart(document.getElementById('eq90').getContext('2d'),{{
 type:'line',
 data:{{labels:{n90_labels},datasets:_n90ds}},
 options:{{
   responsive:true,animation:false,interaction:{{mode:'index',intersect:false}},
   plugins:{{legend:{{labels:{{color:'#e2e8f0',boxWidth:12,font:{{size:11}}}}}},title:{{display:false}}}},
   scales:{{
     x:{{ticks:{{color:'#64748b',maxTicksLimit:8}},grid:{{color:'#1e293b'}}}},
     y:{{ticks:{{color:'#64748b'}},grid:{{color:'#1e293b'}},title:{{display:true,text:'權益(起點=100)',color:'#94a3b8'}}}}
   }}
 }}
}});
}}
</script>
</body></html>"""


def main():
    results, data, eq_map, trades_map = run_all()
    # 共用區段（與追蹤策略無關）：歷史買進訊號、90 天曲線
    buy_rounds = recent_buy_signal_rounds(data)
    ninety = ninety_day_curves(eq_map, n_days=90)
    for tk in TRACKS:
        disp = tk["disp"]
        eq = eq_map.get(disp)
        trades = trades_map.get(disp)
        sig_file, signals = today_signals(tk["orders"])
        sells = recent_sells(trades, n_days=7)
        tm_stats, tm_trades = two_month(eq, trades)
        html = build_html(results, sig_file, signals, sells, tm_stats, tm_trades,
                          buy_rounds, ninety, track=tk)
        with open(tk["file"], "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✅ 已產出 {tk['file']}（追蹤 {disp}，{len(signals)} 買進訊號）")


if __name__ == "__main__":
    main()
