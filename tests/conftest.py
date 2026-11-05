import pytest

from tradility.sources import schwab


@pytest.fixture(autouse=True)
def isolated_token_state(tmp_path, monkeypatch):
    """Each test starts with an empty token cache and its own lock file."""
    monkeypatch.setenv("TRADILITY_TOKEN_LOCK", str(tmp_path / "token.lock"))
    schwab._token_cache.clear()
    yield
    schwab._token_cache.clear()
