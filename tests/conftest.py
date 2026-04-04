import hashlib
import requests
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from diskcache import Cache
from main import JSONDisk


def pytest_configure(config):
    """Crea la directory di cache solo se siamo in ambiente Docker."""
    import os
    cache_dir = os.environ.get("CACHE_DIR", "")
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Sostituisce la cache globale con una temporanea isolata per ogni test."""
    import main
    test_cache = Cache(str(tmp_path / "cache"), disk=JSONDisk)
    monkeypatch.setattr(main, "cache", test_cache)
    yield test_cache
    test_cache.close()


@pytest.fixture
def mock_http_client(mocker):
    """Factory fixture per mock di httpx.AsyncClient + _check_ssrf_safe.

    Uso:
        def test_foo(self, mock_http_client):
            m = mock_http_client(status_code=200)
            # m.client       → AsyncMock del client HTTP
            # m.client_class → mock della classe httpx.AsyncClient (per call_count)
    """
    def _make(status_code=None, side_effect=None):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        if side_effect:
            mock_client.head = AsyncMock(side_effect=side_effect)
        else:
            mock_response = MagicMock()
            mock_response.status_code = status_code
            mock_client.head = AsyncMock(return_value=mock_response)
        client_class = mocker.patch("main.httpx.AsyncClient", return_value=mock_client)
        mocker.patch("main._check_ssrf_safe", new_callable=AsyncMock, return_value="93.184.216.34")
        return SimpleNamespace(client=mock_client, client_class=client_class)
    return _make


@pytest.fixture
def requests_response():
    """Factory fixture per mock di requests.Response.

    Uso:
        def test_foo(self, requests_response):
            resp = requests_response({"data": [...], "links": {}})
            resp_err = requests_response({...}, raise_http_error=True)
    """
    def _make(data, *, raise_http_error=False):
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.raise_for_status = MagicMock()
        if raise_http_error:
            mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        return mock_resp
    return _make


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Resetta il rate limiter prima di ogni test per evitare interferenze tra test."""
    import main
    if hasattr(main.limiter, "_storage"):
        try:
            main.limiter._storage.reset()
        except Exception:
            pass
    yield
