# Version: v2.0
"""
nexus — Multi-Tenant RAG package for the Antigravity agent ecosystem.

Sub-modules
-----------
config    : Service defaults, constants, shared FastMCP instance.
dedup     : Tenant-scoped SHA-256 content hashing.
indexes   : LlamaIndex settings bootstrap and index factories.
tools     : All @mcp.tool()-decorated MCP tool functions.
backends  : Database-specific helpers (memgraph, pgvector).
"""

__version__ = "2.0.0"
