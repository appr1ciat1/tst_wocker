"""Regression tests for the 2026-09-30 / 2026-10-01 order incident.

1. '.TWO' 後綴被改寫成 '3529O' 等不存在代號。
2. 資料商先給出空白的最新一列，Close 被 ffill 補值後偽裝成新交易日。
3. 訂單出廠前沒有任何驗證。

全部離線執行：以假的 yf.download 取代網路。
"""
import numpy as np
import pandas as pd
import pytest

import strategy.ai_strategy as S
from ai_report import validate_orders

IDX = pd.bdate_range('2026-08-03', '2026-10-01')
TICKERS = ['2330', '2454', '2303', '3529', '5274']
OTC = {'3529', '5274'}


def _fake_download_factory(phantom_last=False):
    def fake_download(symbols, start=None, end=None, progress=False, **kw):
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        cols = pd.MultiIndex.from_product([['Close', 'Open', 'High', 'Low', 'Volume'], symbols])
        rng = np.random.default_rng(len(symbols))
        df = pd.DataFrame(rng.uniform(90, 110, (len(IDX), len(cols))), index=IDX, columns=cols)
        for sym in symbols:
            code, suffix = sym.split('.')
            if code in OTC and suffix == 'TW':     # 上櫃股用 .TW 抓不到
                df.loc[:, (slice(None), sym)] = np.nan
        if phantom_last:
            df.iloc[-1] = np.nan                    # 最新一列是空的假 bar
        return df
    return fake_download


@pytest.fixture
def patch_yf(monkeypatch):
    def _apply(phantom_last=False):
        monkeypatch.setattr(S.yf, 'download', _fake_download_factory(phantom_last))
    return _apply


def test_normalize_tw_symbol_strips_only_trailing_suffix():
    assert S.normalize_tw_symbol('3529.TWO') == '3529'
    assert S.normalize_tw_symbol('2330.TW') == '2330'
    assert S.normalize_tw_symbol('00631L.TW') == '00631L'
    assert S.normalize_tw_symbol('2881A.TW') == '2881A'


def test_otc_tickers_keep_their_real_code(patch_yf):
    patch_yf()
    close, *_ = S.fetch_panel_data(TICKERS, days=60, end_date='2026-10-02')
    assert set(close.columns) == set(TICKERS)
    assert not any(c.endswith('O') for c in close.columns)
    assert close['3529'].notna().all()          # .TWO 資料併回正確代號


def test_phantom_last_bar_is_dropped(patch_yf):
    patch_yf(phantom_last=True)
    close, open_, high, low, vol = S.fetch_panel_data(TICKERS, days=60, end_date='2026-10-02')
    assert close.index[-1] == IDX[-2]           # 假的 10/01 不得出現
    assert high.iloc[-1].notna().all()


def test_validate_orders_rejects_unknown_ticker():
    with pytest.raises(RuntimeError, match='3529O'):
        validate_orders([{'ticker': '3529O'}], TICKERS)


def test_validate_orders_rejects_bar_without_high_low():
    d = pd.Timestamp('2026-10-01')
    high = pd.DataFrame({'2330': [np.nan]}, index=[d])
    low = pd.DataFrame({'2330': [np.nan]}, index=[d])
    with pytest.raises(RuntimeError, match='High/Low'):
        validate_orders([{'ticker': '2330'}], TICKERS, high, low, d)


def test_validate_orders_accepts_clean_orders():
    d = pd.Timestamp('2026-09-30')
    high = pd.DataFrame({'2330': [101.0], '3529': [3400.0]}, index=[d])
    low = pd.DataFrame({'2330': [99.0], '3529': [3200.0]}, index=[d])
    orders = [{'ticker': '2330'}, {'ticker': '3529'}]
    assert validate_orders(orders, TICKERS, high, low, d) == orders
