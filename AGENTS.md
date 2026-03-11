# AGENTS.md — MCP Nexus RAG

<!-- Commands for AI agents: testing, building, running -->

**Version:** v3.0

## Services — Full Startup

All Antigravity AI services must be running before using MCP tools.
Use the automation script after a reboot or service restart.

### Quick Start (Recommended)

```bash
# Start all services: Memgraph RAG, Redis, Ollama, Postgres (all Docker)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh

# Start the Code-Graph-RAG realtime watcher (keep Memgraph CGR in sync with code changes)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --watcher

# Start MCP SSE server on port 8765 (for Docker consumers like gravity-claw)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --mcp-sse

# Start HTTP API server on port 8766 (for mission-control Nexus Query page)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --http-api

# Start shared reranker service on port 8767 (saves ~2GB VRAM when both server.py and http_server.py use it)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --reranker
# Then set RERANKER_MODE=remote for server.py and http_server.py consumers

# Start the Nexus RAG sync watcher (auto-ingests CLAUDE.md into Memgraph+pgvector on change)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --rag-sync
# Or directly (logs to stdout):
cd ~/antigravity/projects/mcp-nexus-rag && poetry run python -m nexus.watcher
# Check log:
tail -f /tmp/rag-sync-watcher.log

# Verify everything is healthy
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# Re-index codebase (after major code changes)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --reindex
```

### Service Map

| Service | Start method | Port | Purpose |
|---------|-------------|------|---------|
| **Memgraph RAG** | `docker-compose up -d` | 7689 (bolt) | GraphRAG property graph store |
| **Postgres** | `docker-compose up -d` | 5432 | pgvector vector store |
| **Redis** | `docker-compose up -d` | 6379 | Semantic query cache |
| **Ollama** | `docker-compose up -d` | 11434 | LLM + embeddings |
| **Memgraph CGR** | `start-services.sh` or `docker start memgraph-cgr` | 7688 | Code AST graph |
| **CGR Watcher** | `start-services.sh --watcher` | — | Syncs code changes → Memgraph CGR |
| **RAG Sync Watcher** | `start-services.sh --rag-sync` | — | Auto-ingests CLAUDE.md → Memgraph RAG + pgvector |
| **MCP SSE** | `start-services.sh --mcp-sse` | 8765 | Nexus RAG over SSE (for Docker consumers) |
| **HTTP API** | `start-services.sh --http-api` | 8766 | REST API for mission-control Nexus Query |
| **Reranker** | `start-services.sh --reranker` | 8767 | Shared cross-encoder (saves ~2GB VRAM) |
| **Nexus MCP** | Auto — Claude Code via `.mcp.json` | stdio | RAG tools for agents |
| **Code-Graph-RAG MCP** | Auto — Claude Code via `.mcp.json` | stdio | Code analysis tools |

### MCP Servers (Auto-Start)

MCP servers are **automatically started by Claude Code** on session init using `~/.mcp.json`.
No manual start is needed — they run as stdio processes spawned on demand.

**Nexus MCP** (`server.py`) — launched as:
```bash
/home/turiya/antigravity/projects/mcp-nexus-rag/.venv/bin/python \
  /home/turiya/antigravity/projects/mcp-nexus-rag/server.py
# Env: OLLAMA_URL, MEMGRAPH_URL, REDIS_URL
```

**Code-Graph-RAG MCP** — launched as:
```bash
uv run --directory /home/turiya/code-graph-rag code-graph-rag mcp-server
# Env: TARGET_REPO_PATH=~/antigravity, MEMGRAPH_PORT=7688, CYPHER_MODEL=qwen2.5:3b
```

**All 23 MCP servers** are defined in `~/antigravity/.mcp.json`:
`nexus`, `code-graph-rag`, `github-mcp-server`, `playwright`, `chrome-devtools`,
`sequential-thinking`, `notion`, `fetch`, `filesystem`, `postgres`, `redis`, `git`, `time`, `docker`,
`searxng`, `tavily`, `brave-search`, `mcpbrowser`, `context7`, `sentry`, `linear-server`, `figma`, `supabase`

To reload MCP servers after editing `.mcp.json`, restart the Claude Code session.

### After-Reboot Checklist

```bash
# 1. Start all services (Memgraph RAG, Redis, Ollama, Postgres — all Docker)
#    This also pulls missing Ollama models and starts watchers + SSE + HTTP API servers
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh

# 2. Verify Ollama models are present (auto-pulled by step 1, but verify)
ollama list  # Should show: nomic-embed-text, qllama/bge-reranker-v2-m3, qwen2.5:3b

# 3. Start Mission Control + Gravity Claw
~/antigravity/projects/mission-control/scripts/start-services.sh
cd ~/antigravity/projects/gravity-claw && docker compose up -d --build

# 4. Verify all services
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# 5. MCP servers — no action needed; Claude Code starts them on next session
```

### Fresh Volume Bootstrap (First-Time or After `docker-compose down -v`)

After a volume wipe, Memgraph/pgvector are empty. Run `sync_project_files` to re-ingest CLAUDE.md.

```bash
# 1. Start services normally
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh

# 2. Enable pgvector extension (first time only)
docker exec turiya-postgres psql -U admin -d turiya_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Run sync_project_files via MCP to ingest CLAUDE.md

# 4. Verify data populated
docker exec turiya-postgres psql -U admin -d turiya_memory -c "SELECT count(*) FROM data_nexus_rag;"

# 5. GraphRAG requires qwen2.5:3b — ensure model is pulled before GraphRAG sync runs
ollama list | grep qwen2.5  # Must exist before GraphRAG entity extraction works
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
docker exec turiya-postgres psql -U admin -d turiya_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"
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
docker volume rm mcp-nexus-rag_memgraph_rag_data mcp-nexus-rag_postgres_data
docker-compose up -d
docker exec turiya-postgres psql -U admin -d turiya_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Full reset (including Ollama models)
docker-compose down -v
docker-compose up -d
docker exec turiya-postgres psql -U admin -d turiya_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

## Verify

```bash
# Check all services
docker-compose ps

# Test Memgraph RAG
nc -z localhost 7689 && echo "Memgraph RAG: OK" || echo "Memgraph RAG: FAIL"

# Test pgvector
docker exec turiya-postgres psql -U admin -d turiya_memory -c "SELECT count(*) FROM data_nexus_rag;" 2>/dev/null || echo "pgvector table not yet created (will be created on first ingest)"

# Test Ollama
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"qwen2.5:3b","prompt":"ping","stream":false}'

# Test Redis cache
redis-cli ping                        # Expected: PONG
redis-cli --scan --pattern "nexus:*" | wc -l     # Count cached RAG queries

# Clear Redis cache (all nexus keys — safe to run after code changes)
redis-cli --scan --pattern "nexus:*" | xargs -r redis-cli del && echo "Cache cleared"

# Test reranker loads without errors
poetry run python -c "from nexus.reranker import get_reranker; r = get_reranker(); print('Reranker OK:', type(r).__name__)"

# Full service health check (uses start-services.sh)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# Integrity audit / cleanup (Memgraph RAG + pgvector; dry-run by default)
cd ~/antigravity/projects/mcp-nexus-rag
PYTHONPATH=. poetry run python scripts/safe_cleanup.py
PYTHONPATH=. poetry run python scripts/safe_cleanup.py --apply

# Manual ingest path-normalization probe (absolute path -> relative metadata)
PYTHONPATH=. poetry run python - <<'PY'
import asyncio
from nexus.tools import ingest_document
async def main():
    print(await ingest_document(
        project_id='MCP_NEXUS_RAG',
        scope='USER_SESSIONS',
        file_path='/home/turiya/antigravity/projects/mcp-nexus-rag/TODO.md',
        source_identifier='manual-probe'
    ))
asyncio.run(main())
PY
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
