# Version: v2.0
# ruff: noqa: E402
"""
Nexus RAG MCP Server — entry point.

All logic lives in the nexus/ package.  This file is intentionally thin:
it imports nexus.tools (which registers all @mcp.tool() handlers on the
shared FastMCP instance via side-effect) and exposes every public symbol
for backward-compatible imports by test_rag.py / test_isolation.py.
"""

import nest_asyncio

nest_asyncio.apply()

# Register all MCP tools (side-effect of import)
import nexus.tools  # noqa: F401

# Shared FastMCP application
from nexus.config import PG_TABLE_NAME, logger, mcp, validate_config  # noqa: F401
from nexus.dedup import content_hash as _content_hash  # noqa: F401
from nexus.indexes import (  # noqa: F401
    get_graph_index,
    get_vector_index,
    setup_settings,
)

# ---------------------------------------------------------------------------
# Backward-compatible re-exports (test_rag.py / test_isolation.py)
# ---------------------------------------------------------------------------
from nexus.tools import (  # noqa: F401
    delete_tenant_data,
    get_all_project_ids,
    get_all_tenant_scopes,
    get_graph_context,
    get_vector_context,
    health_check,
    ingest_document,
    ingest_document_batches,
    ingest_graph_document,
    ingest_graph_documents_batch,
    ingest_vector_document,
    ingest_vector_documents_batch,
)


def main() -> None:
    """Run the MCP server via standard stdio transport."""
    for warning in validate_config():
        logger.warning(f"[CONFIG] {warning}")
    mcp.run()


if __name__ == "__main__":
    main()
