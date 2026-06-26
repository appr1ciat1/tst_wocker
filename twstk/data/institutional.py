"""
twstk.data.institutional — 三大法人籌碼資料（★新版）

來源升級為新版 repo 的 GitHub Pages：
    https://appr1ciat1.github.io/tw-institutional-stocker/data

新版相較舊版（voidful）的差異：
- 投信 / 自營商支援「baseline 校正」後的持股推估（ratio 更準）。
- 變化視窗擴充為 5 / 20 / 60 / 120 日。
- 新增「分點券商 broker」系列資料（ranking / stats / trends / trades）。

來源可用環境變數 `TW_INST_BASE_URL` 覆寫（例如改讀本機或你自己的 fork）。
本模組為純資料層：只負責抓取與對齊，不含任何策略邏輯。

對外保留與舊版相同的函式名稱（fetch_inst_timeseries / fetch_inst_rankings /
build_inst_flow_df / get_inst_flow_for_signals），因此既有呼叫端無痛切換到新版。
"""

import os
import json
import urllib.request

import numpy as np
import pandas as pd

# ── 新版資料來源（可用環境變數覆寫）──────────────────────────────
# 註：appr1ciat1 的 GitHub Pages 尚未啟用，故預設改用 raw.githubusercontent.com
#     （已實測可用）。若日後啟用 Pages，改成
#     https://appr1ciat1.github.io/tw-institutional-stocker/data 即可，
#     或直接設環境變數 TW_INST_BASE_URL 覆寫。
DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/appr1ciat1/tw-institutional-stocker/main/docs/data"
)
BASE_URL = os.environ.get("TW_INST_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

TIMEOUT = 15
WINDOWS = (5, 10, 20, 60, 120)  # 10 日可由 timeseries 的持股比率補算


def _fetch_json(url):
    """從 URL 抓 JSON，失敗回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "twstk/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — 資料缺失視為可容忍
        print(f"   ⚠️ 抓取失敗 {url}: {e}")
        return None


# ── 三大法人 ────────────────────────────────────────────────────
def fetch_inst_timeseries(ticker):
    """單檔三大法人持股時序。欄位含 foreign/trust/dealer/three_inst_ratio
    與 three_inst_ratio_change_20。"""
    return _fetch_json(f"{BASE_URL}/timeseries/{ticker}.json")


def fetch_inst_rankings(window=20, direction="up"):
    """三大法人持股變化排名表。window ∈ {5,20,60,120}, direction ∈ {up,down}。"""
    if window not in WINDOWS:
        print(f"   ⚠️ window={window} 非新版支援值 {WINDOWS}，仍嘗試抓取")
    return _fetch_json(f"{BASE_URL}/top_three_inst_change_{window}_{direction}.json")


def fetch_stock_three_inst_latest():
    """新版：全市場最新三大法人持股快照（單一檔，免逐檔抓時序）。"""
    return _fetch_json(f"{BASE_URL}/stock_three_inst_latest.json")


def build_inst_flow_df(tickers, close_df, window=20, verbose=True):
    """
    批次抓多檔三大法人時序，對齊到 close_df 的 (日期 × 代號)。

    Returns
    -------
    inst_flow_df : pd.DataFrame   三大法人持股「變化」矩陣（依 window）
    inst_ratio_df : pd.DataFrame  三大法人持股「比重」矩陣
    """
    flow_by_window, inst_ratio_df = build_inst_flow_windows(
        tickers, close_df, windows=(window,), verbose=verbose,
    )
    return flow_by_window.get(window), inst_ratio_df


def build_inst_flow_windows(tickers, close_df, windows=(5, 10, 20), verbose=True):
    """
    一次下載 timeseries，產生多個三大法人持股變化窗口。

    網站 timeseries 常見欄位為 5/20/60/120 日變化；10 日變化若不存在，
    由 three_inst_ratio 的日序列以 diff(10) 補算，避免把 10 日誤用成 20 日。
    """
    windows = tuple(int(w) for w in windows)
    if verbose:
        joined = ",".join(str(w) for w in windows)
        print(f"🏛️ [新版] 抓取 {len(tickers)} 檔三大法人資料 (windows={joined})...")

    flow_data_by_window = {w: {} for w in windows}
    ratio_data = {}
    success = failed = 0

    for i, ticker in enumerate(tickers):
        series = fetch_inst_timeseries(ticker)
        if not series:
            failed += 1
            continue
        success += 1
        for record in series:
            dt = record.get("date")
            if dt is None:
                continue
            try:
                date_idx = pd.Timestamp(dt)
            except Exception:
                continue

            ratio = record.get("three_inst_ratio", np.nan)
            ratio_data.setdefault(date_idx, {})[ticker] = ratio
            for window in windows:
                change = record.get(f"three_inst_ratio_change_{window}")
                if change is not None:
                    flow_data_by_window[window].setdefault(date_idx, {})[ticker] = change

        if verbose and (i + 1) % 10 == 0:
            print(f"   📦 已處理 {i + 1}/{len(tickers)} 檔...")

    if verbose:
        print(f"   ✅ 三大法人: {success} 檔成功, {failed} 檔失敗")

    if not ratio_data:
        empty = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)
        return {w: empty.copy() for w in windows}, empty.copy()

    inst_ratio_df = pd.DataFrame.from_dict(ratio_data, orient="index")
    inst_ratio_df = inst_ratio_df.reindex(index=close_df.index, columns=close_df.columns)
    ratio_filled = inst_ratio_df.ffill()

    flow_by_window = {}
    for window in windows:
        raw = pd.DataFrame.from_dict(flow_data_by_window[window], orient="index")
        raw = raw.reindex(index=close_df.index, columns=close_df.columns)
        computed = ratio_filled.diff(window)
        flow_by_window[window] = raw.combine_first(computed)

    return flow_by_window, inst_ratio_df


def get_inst_flow_for_signals(tickers, window=20):
    """
    即時信號用：用排名表快速查每檔三大法人變化 + 標籤。

    Returns
    -------
    dict[str, {change, ratio, label}]
    """
    up_list = fetch_inst_rankings(window, "up") or []
    down_list = fetch_inst_rankings(window, "down") or []

    lookup = {}
    for item in up_list:
        lookup[item["code"]] = {
            "change": item.get("change", 0.0),
            "ratio": item.get("three_inst_ratio", 0.0),
        }
    for item in down_list:
        lookup[item["code"]] = {
            "change": -abs(item.get("change", 0.0)),
            "ratio": item.get("three_inst_ratio", 0.0),
        }

    def _label(change):
        if change > 2.0:
            return "🟢 大買"
        if change > 0.5:
            return "🟡 小買"
        if change < -2.0:
            return "🔴 大賣"
        if change < -0.5:
            return "🟠 小賣"
        return "⚪ 中性"

    result = {}
    for t in tickers:
        if t in lookup:
            info = lookup[t]
            result[t] = {
                "change": info["change"],
                "ratio": info["ratio"],
                "label": _label(info["change"]),
            }
        else:
            result[t] = {"change": 0.0, "ratio": 0.0, "label": "⚪ 無資料"}
    return result


# ── 分點券商 broker（新版新增）──────────────────────────────────
def fetch_broker_ranking():
    """分點券商買賣超排名。"""
    return _fetch_json(f"{BASE_URL}/broker_ranking.json")


def fetch_broker_stats():
    """分點券商統計（命中率 / 勝率等）。"""
    return _fetch_json(f"{BASE_URL}/broker_stats.json")


def fetch_broker_trends():
    """分點券商趨勢。"""
    return _fetch_json(f"{BASE_URL}/broker_trends.json")


def fetch_broker_trades_latest():
    """最新分點券商交易明細。"""
    return _fetch_json(f"{BASE_URL}/broker_trades_latest.json")


def fetch_target_broker_trades():
    """重點/目標券商交易明細。"""
    return _fetch_json(f"{BASE_URL}/target_broker_trades.json")
