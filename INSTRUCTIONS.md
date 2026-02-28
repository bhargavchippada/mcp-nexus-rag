# MCP Nexus RAG: Instructions & Maintenance

This submodule contains the FastMCP configuration and Python logic to support Multi-Tenant GraphRAG index generation and standard Vector RAG context retrieval using LlamaIndex, Ollama, Qdrant, and Neo4j.

## Core Services & Hardware Dependency

This plugin relies on the `docker-compose.yml` provided in this directory.

- **Postgres (pgvector)**: `localhost:5432`
- **Neo4j**: `bolt://localhost:7687` (neo4j/password123)
- **Ollama**: `http://localhost:11434`
- **Qdrant**: `localhost:6333`

To start or restart the infrastructure from this directory:

```bash
docker-compose down && docker-compose up -d
```

### Data-Only Reset (Preserve Ollama Models)

To delete only the database data (Neo4j, Qdrant, and Postgres) while preserving your Ollama models (saving you from that 5GB download), follow this specific sequence:

1. **Stop the containers**

   ```bash
   docker-compose down
   ```

2. **Delete the databases** (Replace 'mcp-nexus-rag' if your volume prefix name is different based on `docker volume ls`)

   ```bash
   docker volume rm mcp-nexus-rag_neo4j_data
   docker volume rm mcp-nexus-rag_qdrant_data
   docker volume rm mcp-nexus-rag_postgres_data
   ```

3. **Restart the stack** (Ollama will still have its models)

   ```bash
   docker-compose up -d
   ```

**Verification:**
After the restart, you can verify the databases are empty but the models are present:

- Check Ollama Models: `docker exec -it turiya-ollama ollama list` (Should show Llama 3.1 immediately).
- Check Neo4j: Visit <http://localhost:7474>. (Should show 0 nodes).
- Check Qdrant: Visit <http://localhost:6333/dashboard>. (Should show 0 collections).

## Setup Dependencies

We use `poetry` for dependency management:

```bash
poetry install
```

## Adding the custom MCP to Antigravity (Cursor / Claude Desktop)

To enable agents to use the ingest and context retrieval tools, add this configuration to your MCP settings file (e.g. `claude_desktop_config.json` or Cursor MCP settings via the GUI).

*Note: Replace `/path/to/...` with your actual absolute path to `projects/mcp-nexus-rag` inside your WSL/OS.*

**JSON Configuration:**

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

## Testing & Visualizing

- **Testing Script**: Run `poetry run python test_rag.py` to inject test graphs and vectors into different project IDs and query them.
- **Querying**: Agents will use the `get_graph_context`, `get_vector_context`, etc., through the MCP interface naturally to append relevant history.
- **Visualizer**: To view the graphical output, run `poetry run python visualizer.py`. It will launch a dash web server visualizing the local Neo4j relationships.

## Helpful Commands & Examples

### Docker Commands

- `docker-compose logs -f`: To view the logs of all containers in real time. Very useful for watching the `ollama-init` container pull models.
- `docker exec -it turiya-ollama ollama list`: To verify the models built/pulled successfully inside the active Ollama container.
- `docker-compose ps`: To check the running status (healthy/unhealthy/exited) of all the containers in the stack.
- `docker-compose down -v`: **WARNING:** Completely wipe ALL data including Ollama models, Neo4j graph, and Qdrant vectors. Use only for a clean slate.

### Test Ollama Connection

To ensure the LLM is responding appropriately (specifically `llama3.1:8b`), you can run the following test `curl` command directly from your terminal:

```bash
curl -X POST http://localhost:11434/api/generate -d '{
  "model": "llama3.1:8b",
  "prompt": "why is the sky blue? Answer in one short sentence.",
  "stream": false
}'
```

### Inspecting PostgreSQL / PGVector

If you need to connect directly to the PostgreSQL database to inspect the memory embeddings, run:

```bash
docker exec -it turiya-postgres psql -U admin -d turiya_memory
```

### MCP Inspector

If you want to test the MCP server interactively in your browser before plugging it into Claude or Cursor, you can use the official MCP Inspector. From within the `mcp-nexus-rag` directory (`poetry` environment must be set up):

```bash
npx @modelcontextprotocol/inspector poetry run python server.py
```
