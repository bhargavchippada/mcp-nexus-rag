# Version: v1.0
import asyncio
import pytest
from server import get_context

@pytest.mark.asyncio
async def test_get_context_isolation():
    """
    Test that the RAG retrieval adheres strictly to the tenant_scope and project_id rules.
    """
    # Mock behavior until Neo4j DB is populated
    response_trading = await get_context("market trends", "TRADING_BOT", "WEB_RESEARCH")
    response_portal = await get_context("market trends", "WEB_PORTAL", "WEB_RESEARCH")
    
    assert "TRADING_BOT" in response_trading
    assert "WEB_PORTAL" in response_portal
    assert response_trading != response_portal
