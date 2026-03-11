# Version: v2.0
"""
tests/conftest.py — Shared fixtures and mock helpers for the Nexus RAG test suite.

Centralises the mock-builder functions previously duplicated between
test_unit.py and test_integration.py so each test module stays lean.

v2.0: Migrated from Neo4j/Qdrant to Memgraph/pgvector backends.
"""

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Memgraph mock builders — re-usable across all test modules
# ---------------------------------------------------------------------------


def make_graph_driver(session_records=None):
    """Build a MagicMock Memgraph driver whose session.run() returns *session_records*.

    Args:
        session_records: Iterable returned by session.run(); defaults to [].

    Returns:
        Tuple(mock_driver, mock_session).
    """
    mock_session = MagicMock()
    mock_session.run.return_value = session_records or []
    mock_driver = MagicMock()
    mock_driver.__enter__ = lambda s: mock_driver
    mock_driver.__exit__ = MagicMock(return_value=False)
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver, mock_session


def make_graph_driver_with_single(single_return):
    """Build a MagicMock Memgraph driver whose session.run().single() returns *single_return*.

    Args:
        single_return: Value returned by result.single().

    Returns:
        mock_driver.
    """
    mock_session = MagicMock()
    mock_session.run.return_value.single.return_value = single_return
    mock_driver = MagicMock()
    mock_driver.__enter__ = lambda s: mock_driver
    mock_driver.__exit__ = MagicMock(return_value=False)
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


# ---------------------------------------------------------------------------
# Pytest fixtures exposed to all test modules
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def disable_cache(monkeypatch):
    """Disable Redis cache for all unit/integration tests.

    Prevents real Redis reads/writes from polluting test isolation.
    Tests that explicitly test caching should override this with their own mock.
    """
    import nexus.tools as tools_module

    monkeypatch.setattr(tools_module.cache_module, "get_cached", lambda *a, **kw: None)
    monkeypatch.setattr(tools_module.cache_module, "set_cached", lambda *a, **kw: None)


@pytest.fixture()
def mock_graph_driver(monkeypatch):
    """Fixture that returns a helper that patches graph_backend.get_driver."""
    from nexus.backends import memgraph as graph_backend

    def _factory(session_records=None):
        driver, session = make_graph_driver(session_records)
        monkeypatch.setattr(graph_backend, "get_driver", lambda: driver)
        return driver, session

    return _factory


@pytest.fixture()
def mock_pgvector_conn(monkeypatch):
    """Fixture that injects a MagicMock psycopg2 connection into pgvector_backend."""
    from nexus.backends import pgvector as vector_backend

    conn = MagicMock()
    monkeypatch.setattr(vector_backend, "get_connection", lambda *a, **kw: conn)
    return conn
