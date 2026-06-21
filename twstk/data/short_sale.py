"""
twstk.data.short_sale — 借券賣出(SBL)餘額資料（法人/外資空方訊號）

來源：FinMind `TaiwanDailyShortSaleBalances`（含融券 Margin 與借券 SBL 兩套）。
重點欄位：`SBLShortSalesCurrentDayBalance`（借券賣出當日餘額）—— 法人空方,
與散戶「融券」不同。IC 分析顯示其對未來報酬有顯著負相關(法人放空有效)。

⚠️ 此資料約只回溯 1 年(2025-06 起),屬短歷史,因子需保守使用。

提供：
- fetch_sbl_balances(tickers, start_date, end_date) -> (日期 × 代號) 借券賣出餘額矩陣
快取於 artifacts/sbl_balances_cache.pkl,支援每日增量更新(只補抓缺漏尾段)。
純資料層：只負責抓取與對齊,不含因子/策略邏輯。
"""

import os
import ssl
import json
import time
import pickle
import urllib.request
import urllib.parse
from datetime import date, timedelta

import numpy as np
import pandas as pd

_CTX = ssl.create_default_context()
_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT  # 同 institutional：避免 OpenSSL3 嚴格檢查擋下

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
DATASET = "TaiwanDailyShortSaleBalances"
SBL_FIELD = "SBLShortSalesCurrentDayBalance"
EARLIEST = "2025-06-01"   # 來源最早約此日
CACHE = os.path.join("artifacts", "sbl_balances_cache.pkl")
_REQUEST_PAUSE = 0.25      # 尊重 FinMind 速率


def _fetch_one(ticker, start_date, end_date):
    params = {"dataset": DATASET, "data_id": ticker,
              "start_date": start_date, "end_date": end_date}
    url = FINMIND_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "twstk/1.0"})
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ SBL 抓取失敗 {ticker}: {e}")
        return []
    return d.get("data", []) if d.get("status") == 200 else []


def _load_cache():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                return pickle.load(f)
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_cache(df):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump(df, f)


def fetch_sbl_balances(tickers, start_date=None, end_date=None, days=400,
                       verbose=True) -> pd.DataFrame:
    """
    回傳 (日期 × 代號) 的借券賣出餘額矩陣。

    Parameters
    ----------
    tickers : list[str]
    start_date, end_date : str, optional
    days : int
        未給 start_date 時,回溯天數。

    每日增量：若快取已涵蓋大部分區間,只補抓最新缺漏(以最後日期起),降低 API 用量。
    """
    end_date = end_date or date.today().isoformat()
    if start_date is None:
        start_date = (pd.Timestamp(end_date) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    start_date = max(start_date, EARLIEST)

    cache = _load_cache()
    need_full = cache is None
    fetch_start = start_date

    if not need_full:
        missing_cols = [t for t in tickers if t not in cache.columns]
        cache_max = cache.index.max()
        # 增量：只補抓快取最後日期之後(留 5 天緩衝重抓)；有新代號則整段補
        if missing_cols:
            need_full = True
        elif pd.Timestamp(end_date) > pd.Timestamp(cache_max):
            fetch_start = (pd.Timestamp(cache_max) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        else:
            # 快取已足夠
            return cache.reindex(columns=tickers).loc[
                (cache.index >= pd.Timestamp(start_date)) & (cache.index <= pd.Timestamp(end_date))
            ]

    fetch_start = max(fetch_start, EARLIEST)
    if verbose:
        mode = "全量" if need_full else "增量"
        print(f"📥 [SBL] {mode}抓取 {len(tickers)} 檔借券賣出餘額 "
              f"({fetch_start} → {end_date}) ...")

    data = {}
    ok = 0
    for i, t in enumerate(tickers):
        rows = _fetch_one(t, fetch_start, end_date)
        if rows:
            ok += 1
            for r in rows:
                try:
                    dt = pd.Timestamp(r["date"])
                except Exception:
                    continue
                data.setdefault(dt, {})[t] = r.get(SBL_FIELD, np.nan)
        if verbose and (i + 1) % 25 == 0:
            print(f"   {i + 1}/{len(tickers)} (ok={ok})")
        time.sleep(_REQUEST_PAUSE)

    new_df = pd.DataFrame.from_dict(data, orient="index").sort_index()

    if need_full or cache is None:
        merged = new_df
    else:
        # 合併：新資料覆蓋舊(處理修正),保留舊有未重抓的部分
        merged = new_df.combine_first(cache)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    _save_cache(merged)
    if verbose:
        print(f"   ✅ [SBL] 完成 ok={ok}，快取 {CACHE}（{merged.shape[0]} 日 × {merged.shape[1]} 檔）")

    return merged.reindex(columns=tickers).loc[
        (merged.index >= pd.Timestamp(start_date)) & (merged.index <= pd.Timestamp(end_date))
    ]
