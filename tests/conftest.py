# Version: v1.0
"""
tests/conftest.py — Shared fixtures and mock helpers for the Nexus RAG test suite.

Centralises the mock-builder functions previously duplicated between
test_unit.py and test_integration.py so each test module stays lean.
"""

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Neo4j mock builders — re-usable across all test modules
# ---------------------------------------------------------------------------


def make_neo4j_driver(session_records=None):
    """Build a MagicMock Neo4j driver whose session.run() returns *session_records*.

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


def make_neo4j_driver_with_single(single_return):
    """Build a MagicMock Neo4j driver whose session.run().single() returns *single_return*.

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


@pytest.fixture()
def mock_neo4j_driver(monkeypatch):
    """Fixture that returns a helper that patches neo4j_backend.neo4j_driver."""
    from nexus.backends import neo4j as neo4j_backend

    def _factory(session_records=None):
        driver, session = make_neo4j_driver(session_records)
        monkeypatch.setattr(neo4j_backend, "neo4j_driver", lambda: driver)
        return driver, session

    return _factory


@pytest.fixture()
def mock_qdrant_client(monkeypatch):
    """Fixture that injects a MagicMock QdrantClient into qdrant_backend.get_client."""
    from nexus.backends import qdrant as qdrant_backend

    client = MagicMock()
    monkeypatch.setattr(qdrant_backend, "get_client", lambda *a, **kw: client)
    return client
