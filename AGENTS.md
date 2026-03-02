# AGENTS.md — MCP Nexus RAG

<!-- Commands for AI agents: testing, building, running -->

**Version:** v1.2

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
```
