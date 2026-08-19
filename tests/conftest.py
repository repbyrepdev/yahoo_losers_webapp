"""Shared test configuration.

Every test runs against the in-memory cache only. With REDIS_URL set in the
environment, TTLCache.get() would read Redis before _local -- so popping
_local would not remove stale entries, seeded keys would leak into a shared
Redis, and cache-sensitive tests would flake in exactly the environments
that matter (CR, PRs 49 and 52).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data  # noqa: E402


@pytest.fixture(autouse=True)
def _in_memory_cache_only(monkeypatch):
    # Capture the real cache at setup: some tests monkeypatch
    # market_data._cache with a fake that has no _local, and teardown must
    # clear the original object, not whatever a test swapped in.
    cache = market_data._cache
    monkeypatch.setattr(cache, "_redis", None)
    cache._local.clear()
    yield
    cache._local.clear()


@pytest.fixture(autouse=True)
def _no_network_in_sources(monkeypatch):
    """Tests must never reach a real provider through the sources layer.

    Any unmocked call fails loudly instead of hanging on a 20s timeout.
    Individual tests monkeypatch sources.requests themselves when they need
    a fake response.
    """
    import sources

    def _blocked(*args, **kwargs):
        raise AssertionError("network call escaped a test via sources.requests")

    monkeypatch.setattr(sources.requests, "get", _blocked)
    monkeypatch.setattr(sources.requests, "post", _blocked)
