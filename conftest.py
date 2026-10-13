"""測試一律離線：禁止向交易所發出請求，並把行情快取導到暫存目錄。"""
import pytest


@pytest.fixture(autouse=True)
def _no_exchange_network(monkeypatch, tmp_path):
    import strategy.tw_quotes as TQ

    def _blocked(url, timeout=60):
        raise RuntimeError(f'tests are offline: {url}')

    monkeypatch.setattr(TQ, '_get_json', _blocked)
    monkeypatch.setenv('TW_QUOTE_CACHE', str(tmp_path / 'tw_quotes'))
