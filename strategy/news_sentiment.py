"""
新聞情緒因子模組 (News Sentiment Factor)

從 voidful/tw_news_stocker GitHub Pages 抓取新聞情緒排名，
用於即時信號標注。

數據來源:
- https://voidful.github.io/tw_news_stocker/
- docs/data/leaderboard_{d}d.csv (d = 1, 3, 5, 10, 30, 60)
- data/news_log.jsonl (歷史累積)

情緒打分規則 (來自 tw_news_stocker):
- FlashText 關鍵字抽取 + 距離窗 + 否定規則
- 每則新聞每家公司 |score| ≤ 2
- 讓步/條件語句加權
"""

import csv
import io
import json
import urllib.request

RAW_DATA_URL = "https://raw.githubusercontent.com/voidful/tw_news_stocker/main/docs/data"
PAGES_DATA_URL = "https://voidful.github.io/tw_news_stocker/data"
LEGACY_RAW_URL = "https://raw.githubusercontent.com/voidful/tw_news_stocker/main"
TIMEOUT = 15


def _fetch_text(url, quiet=False):
    """從 URL 抓文字，失敗回 None。"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'tw_stocker/1.0'})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        if not quiet:
            print(f"   ⚠️ 新聞數據抓取失敗 {url}: {e}")
        return None


def _fetch_json(url):
    """從 URL 抓 JSON，失敗回 None。"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'tw_stocker/1.0'})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"   ⚠️ 新聞數據抓取失敗 {url}: {e}")
        return None


def fetch_news_leaderboard(days=5):
    """
    抓取新聞情緒排名 CSV。

    Parameters
    ----------
    days : int
        時間視窗 (1, 3, 5, 10, 30, 60)

    Returns
    -------
    list[dict] or None
        排名列表，每筆含 ticker, name, score 等
    """
    urls = [
        f"{RAW_DATA_URL}/leaderboard_{days}d.csv",
        f"{PAGES_DATA_URL}/leaderboard_{days}d.csv",
        f"{LEGACY_RAW_URL}/outputs/leaderboard_{days}d.csv",
    ]
    text = None
    for url in urls:
        text = _fetch_text(url, quiet=True)
        if text:
            break
    if text is None:
        print(f"   ⚠️ 新聞數據抓取失敗 leaderboard_{days}d.csv")
        return None

    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    results = []
    for row in reader:
        try:
            results.append({
                'ticker': row.get('code', row.get('ticker', '')).strip(),
                'name': row.get('name', '').strip(),
                'score': float(row.get('score', row.get('sentiment_score', 0))),
            })
        except (ValueError, KeyError):
            continue

    return results


def get_news_sentiment_for_signals(tickers, days=5):
    """
    為即時信號取得新聞情緒標注。

    Parameters
    ----------
    tickers : list[str]
        候選股票代號
    days : int
        時間視窗 (default 5 = 近 5 天)

    Returns
    -------
    dict
        {ticker: {'score': float, 'label': str}}
    """
    leaderboard = fetch_news_leaderboard(days)

    lookup = {}
    if leaderboard:
        for item in leaderboard:
            t = item.get('ticker', '')
            if t:
                lookup[t] = item.get('score', 0.0)

    result = {}
    for t in tickers:
        score = lookup.get(t, 0.0)
        if score > 3.0:
            label = '🟢 強正面'
        elif score > 1.0:
            label = '🟡 正面'
        elif score < -3.0:
            label = '🔴 強負面'
        elif score < -1.0:
            label = '🟠 負面'
        else:
            label = '⚪ 中性'

        result[t] = {
            'score': score,
            'label': label,
        }

    return result
