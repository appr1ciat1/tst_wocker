#!/usr/bin/env python3
"""資料源看門狗：檢查法人籌碼資料源（tw-institutional-stocker 的 GitHub Pages）是否停更。

起因：2026-10-14 起，GitHub 以「Actions 用量規模」為由停用了 fork 上的 workflow，
tw-institutional-stocker 不再更新；paper_trading.html 的法人籌碼排行顯示舊資料兩週，
沒有任何警訊。這支腳本由獨立 workflow 每個交易日執行，停更就讓 run 變紅（GitHub 寄信）。

判定：資料日之後、報表參考日之前（不含），XTAI 交易日數
  0 → 正常；1 → 警告（可能只是當天還沒更新）；≥ 2 → 錯誤（確定停更）。
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from inst_widget import INST_DATA_BASE  # noqa: E402

FILES = ["top_foreign_change_5_up.json", "top_three_inst_net_5_up.json", "main_force_rankings.json"]


def data_date(payload):
    if isinstance(payload, dict):
        return payload.get("date")
    return payload[0].get("date") if payload else None


def missed_sessions(date_str, ref_date, calendar):
    """資料日之後、ref_date 之前（不含兩端）的交易日。"""
    import pandas as pd
    start = pd.Timestamp(date_str) + pd.Timedelta(days=1)
    end = pd.Timestamp(ref_date) - pd.Timedelta(days=1)
    if end < start:
        return []
    return [pd.Timestamp(s).strftime("%Y-%m-%d") for s in calendar.sessions_in_range(start, end)]


def verdict(n_missed):
    return "ok" if n_missed == 0 else ("warning" if n_missed == 1 else "error")


def main():
    import exchange_calendars as xcals
    from strategy.data_quality import report_reference_date
    cal = xcals.get_calendar("XTAI")
    ref = report_reference_date()
    worst = "ok"
    lines = [f"## 法人籌碼資料源檢查（參考日 {ref}）", "", "| 檔案 | 資料日 | 之後漏掉的交易日 | 判定 |", "|---|---|---|---|"]
    for name in FILES:
        url = f"{INST_DATA_BASE}/{name}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tst-wocker-watchdog"}),
                                        timeout=60) as r:
                d = data_date(json.loads(r.read().decode("utf-8")))
        except Exception as e:
            print(f"::error::無法讀取 {url}: {type(e).__name__}: {e}")
            lines.append(f"| {name} | 讀取失敗 | – | ❌ |")
            worst = "error"
            continue
        if not d:
            print(f"::error::{name} 沒有資料日期")
            lines.append(f"| {name} | 無日期 | – | ❌ |")
            worst = "error"
            continue
        missed = missed_sessions(d, ref, cal)
        v = verdict(len(missed))
        mark = {"ok": "✅", "warning": "⚠️", "error": "❌"}[v]
        lines.append(f"| {name} | {d} | {len(missed)}：{', '.join(missed[-5:]) or '無'} | {mark} |")
        if v == "error":
            print(f"::error::法人籌碼資料源停更：{name} 資料日 {d}，之後 {len(missed)} 個交易日沒有更新。"
                  f"請到 https://github.com/appr1ciat1/tw-institutional-stocker/actions 檢查 workflow 是否被停用")
            worst = "error"
        elif v == "warning" and worst == "ok":
            print(f"::warning::{name} 資料日 {d}，落後 1 個交易日（可能尚未更新）")
            worst = "warning"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if worst == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
