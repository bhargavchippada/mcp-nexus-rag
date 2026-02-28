# Version: v1.8
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
from nexus.config import mcp

# ---------------------------------------------------------------------------
# Backward-compatible re-exports (test_rag.py / test_isolation.py)
# ---------------------------------------------------------------------------
from nexus.tools import (  # noqa: F401
    ingest_graph_document,
    get_graph_context,
    ingest_vector_document,
    get_vector_context,
    health_check,
    get_all_project_ids,
    get_all_tenant_scopes,
    delete_tenant_data,
)
from nexus.backends.neo4j import (  # noqa: F401
    neo4j_driver as _neo4j_driver,
    get_distinct_metadata as get_distinct_metadata_neo4j,
    delete_data as delete_data_neo4j,
    is_duplicate as _is_duplicate_neo4j,
)
from nexus.backends.qdrant import (  # noqa: F401
    scroll_field as _scroll_qdrant_field,
    get_distinct_metadata as get_distinct_metadata_qdrant,
    delete_data as delete_data_qdrant,
    is_duplicate as _is_duplicate_qdrant,
)
from nexus.dedup import content_hash as _content_hash  # noqa: F401
from nexus.indexes import setup_settings, get_graph_index, get_vector_index  # noqa: F401
from nexus.config import COLLECTION_NAME, ALLOWED_META_KEYS as _ALLOWED_META_KEYS  # noqa: F401


def main() -> None:
    """Run the MCP server via standard stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
