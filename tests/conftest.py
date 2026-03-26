import pytest
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
