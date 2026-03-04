# AGENTS.md — MCP Nexus RAG

<!-- Commands for AI agents: testing, building, running -->

**Version:** v1.7

## Services — Full Startup

All Antigravity AI services must be running before using MCP tools.
Use the automation script after a reboot or service restart.

### Quick Start (Recommended)

```bash
# Start all services in one command: Neo4j, Qdrant, Redis, Ollama, Postgres, Memgraph
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh

# Start the Code-Graph-RAG realtime watcher (keep Memgraph in sync with code changes)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --watcher

# Start the Nexus RAG sync watcher (auto-ingests core docs into Neo4j+Qdrant on change)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --rag-sync
# Or directly (logs to stdout):
cd ~/antigravity/projects/mcp-nexus-rag && poetry run python -m nexus.watcher
# Check log:
tail -f /tmp/rag-sync-watcher.log

# Verify everything is healthy
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health
```

### Service Map

| Service | Start method | Port | Purpose |
|---------|-------------|------|---------|
| **Neo4j** | `docker-compose up -d` | 7687 (bolt), 7474 (http) | GraphRAG store |
| **Qdrant** | `docker-compose up -d` | 6333 | Vector store |
| **Redis** | `docker-compose up -d` | 6379 | Semantic query cache |
| **Ollama** | `docker-compose up -d` | 11434 | LLM + embeddings |
| **Postgres** | `docker-compose up -d` | 5432 | Reserved (pgvector) |
| **Memgraph** | `start-services.sh` or `docker start memgraph-cgr` | 7688 | Code AST graph |
| **CGR Watcher** | `start-services.sh --watcher` | — | Syncs code changes → Memgraph |
| **RAG Sync Watcher** | `start-services.sh --rag-sync` | — | Auto-ingests core docs → Neo4j+Qdrant |
| **Nexus MCP** | Auto — Claude Code via `.mcp.json` | stdio | RAG tools for agents |
| **Code-Graph-RAG MCP** | Auto — Claude Code via `.mcp.json` | stdio | Code analysis tools |

### MCP Servers (Auto-Start)

MCP servers are **automatically started by Claude Code** on session init using `~/.mcp.json`.
No manual start is needed — they run as stdio processes spawned on demand.

**Nexus MCP** (`server.py`) — launched as:
```bash
/home/turiya/antigravity/projects/mcp-nexus-rag/.venv/bin/python \
  /home/turiya/antigravity/projects/mcp-nexus-rag/server.py
# Env: OLLAMA_URL, NEO4J_URL, QDRANT_URL, REDIS_URL
```

**Code-Graph-RAG MCP** — launched as:
```bash
uv run --directory /home/turiya/code-graph-rag code-graph-rag mcp-server
# Env: TARGET_REPO_PATH=~/antigravity, MEMGRAPH_PORT=7688, CYPHER_MODEL=llama3.1:8b
```

**All 15 MCP servers** are defined in `~/antigravity/.mcp.json`:
`nexus`, `code-graph-rag`, `github-mcp-server`, `puppeteer`, `playwright`, `chrome-devtools`,
`sequential-thinking`, `notion`, `fetch`, `filesystem`, `postgres`, `redis`, `git`, `time`, `docker`

To reload MCP servers after editing `.mcp.json`, restart the Claude Code session.

### After-Reboot Checklist

```bash
# 1. Start Docker services (Neo4j, Qdrant, Redis, Ollama, Postgres)
cd ~/antigravity/projects/mcp-nexus-rag && docker-compose up -d

# 2. Start Memgraph (Code-Graph-RAG backend)
docker start memgraph-cgr || docker run -d --name memgraph-cgr \
  -p 7688:7687 -p 7445:7444 --restart unless-stopped memgraph/memgraph-mage

# 3. Start realtime watcher (Memgraph ← code changes)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --watcher

# 4. Verify all services
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# 5. MCP servers — no action needed; Claude Code starts them on next session
```

### Code-Graph-RAG Index Management

```bash
# Check what's indexed (Memgraph graph stats)
cd ~/code-graph-rag && .venv/bin/python -c "
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.memgraph_connection import MemgraphConnection
conn = MemgraphConnection(host='localhost', port=7688)
print(conn.query('MATCH (n) RETURN labels(n)[0] as type, count(*) as cnt ORDER BY cnt DESC'))
"

# Force full re-index (delete hash cache first)
rm -f ~/code-graph-rag/.cgr-hash-cache.json
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --reindex

# Watcher log inspection
tail -50 /tmp/cgr-watcher.log
grep -i "error\|warn" /tmp/cgr-watcher.log | tail -20
```

## Setup

```bash
poetry install --with dev
docker-compose up -d
```

## Run

```bash
npx @modelcontextprotocol/inspector poetry run python server.py
```

## Test

```bash
# Fast unit tests (2.3s, no live services required)
poetry run pytest

# Integration tests (slow, requires docker-compose)
poetry run pytest -m integration -v

# All tests including integration
poetry run pytest -m '' -v

# With coverage
poetry run pytest --cov=nexus --cov=server --cov-report=term-missing
```

## Lint

```bash
poetry run ruff check . --fix
poetry run ruff format .
```

## Reset

```bash
# Soft reset (data only, preserves Ollama models)
docker-compose down
docker volume rm mcp-nexus-rag_neo4j_data mcp-nexus-rag_qdrant_data
docker-compose up -d

# Full reset (including Ollama models)
docker-compose down -v
docker-compose up -d
```

## Verify

```bash
# Check all services
docker-compose ps

# Test Neo4j
curl http://localhost:7474 || echo "Neo4j not responding"

# Test Qdrant
curl http://localhost:6333/collections || echo "Qdrant not responding"

# Test Ollama
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:8b","prompt":"ping","stream":false}'

# Test Redis cache
redis-cli ping                        # Expected: PONG
redis-cli --scan --pattern "nexus:*" | wc -l     # Count cached RAG queries

# Clear Redis cache (all nexus keys — safe to run after code changes)
redis-cli --scan --pattern "nexus:*" | xargs -r redis-cli del && echo "Cache cleared"

# Test reranker loads without errors
poetry run python -c "from nexus.reranker import get_reranker; r = get_reranker(); print('Reranker OK:', type(r).__name__)"

# Full service health check (uses start-services.sh)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# Integrity audit / cleanup (Neo4j + Qdrant; dry-run by default)
cd ~/antigravity/projects/mcp-nexus-rag
PYTHONPATH=. poetry run python scripts/safe_cleanup.py
PYTHONPATH=. poetry run python scripts/safe_cleanup.py --apply
```

## Watcher (Code-Graph-RAG)

```bash
# Start/restart the realtime file watcher
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --watcher

# Check watcher log
tail -f /tmp/cgr-watcher.log

# Manual watcher start (if script unavailable)
cd ~/code-graph-rag
nohup .venv/bin/python realtime_updater.py ~/antigravity --host localhost --port 7688 > /tmp/cgr-watcher.log 2>&1 &
```
