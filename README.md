# MCP Nexus RAG

Multi-Tenant GraphRAG Memory Server for Google Antigravity. Wraps LlamaIndex PropertyGraphIndex logic over Neo4j.

## Instructions & Maintenance

This submodule contains the FastMCP configuration and Python logic to support Multi-Tenant GraphRAG index generation and context retrieval using LlamaIndex, Ollama, and Neo4j.

### Core Services & Hardware Dependency

This plugin relies on the root Antigravity `docker-compose.yml` to be running.

- **Postgres (pgvector)**: `localhost:5432`
- **Neo4j**: `bolt://localhost:7687` (neo4j/password123)
- **Ollama**: `http://localhost:11434`
- **Qdrant**: `localhost:6333`

To restart the infrastructure from the workspace root (Warning: This deletes the graph/vectors!):

```bash
docker-compose down -v
docker-compose up -d
```

### Adding the custom MCP to Antigravity (Cursor / Claude Desktop)

To enable agents to use the `ingest_document` and `get_context` tools, add this configuration to your MCP settings file (e.g. `claude_desktop_config.json` or Cursor MCP settings via the GUI).
*Note: Replace `/path/to/...` with your actual absolute path to `projects/mcp-nexus-rag` inside your WSL/OS.*

```json
{
  "mcpServers": {
    "nexus-rag": {
      "command": "poetry",
      "args": [
        "run",
        "python",
        "/path/to/antigravity/projects/mcp-nexus-rag/server.py"
      ]
    }
  }
}
```

## System Architecture & Design

### Standard Vector RAG (Qdrant) vs GraphRAG (Neo4j)

This system utilizes a dual-memory approach to balance speed and comprehension:

1. **Vector RAG (Qdrant)**: Used for standard dense semantic search. It's fast, highly effective for factual Q&A, and finding exact code snippets or standard log retrieval. It uses `nomic-embed-text` through Ollama.
2. **GraphRAG (Neo4j)**: Used for understanding complex relationships across multiple documents or codebases. It is better for overarching architecture queries, tracing complex state bugs, or summarizing complex entities. It uses `llama3.1:8b` running locally to build the property graph.

### Multi-Tenant Scoping Strategy

To prevent hallucination and contamination between different projects or areas of code, every document ingestion and query **must** contain strict metadata filters:

- **`project_id`**: The high-level root of the data. For example: `TRADING_BOT`, `WEB_PORTAL`, or `AUTONOMOUS_SWARM`.
- **`tenant_scope`**: Exploring the isolated domain within a project. For example:
  - `CORE_CODE` (Source text and algorithms)
  - `SYSTEM_LOGS` (Runtime traces, tracebacks, analytics)
  - `WEB_RESEARCH` (External documentation, market news)

**Querying logic:** When an agent queries `get_graph_context` or `get_vector_context`, they provide the `project_id` and `scope`. The database securely restricts its vector search or traversal strictly to nodes mathematically tagged with that tuple, ensuring zero crosstalk between your quantitative trading logic and your frontend web CSS.

### Embeddings and Reranking Design

- All embeddings are calculated locally on your RTX 5090 using **`nomic-embed-text`** via Ollama API to guarantee zero data leakage.
- *Future Enhancement:* Output nodes retrieved by Qdrant or Neo4j will be dynamically re-scored and prioritized using the local **`bge-reranker-v2-m3`** cross-encoder model to dramatically increase relevance before being injected back into the LLM context window.
