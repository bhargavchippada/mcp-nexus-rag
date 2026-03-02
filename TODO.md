# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v1.1

## Hardening

- [ ] Exception message sanitization (return generic errors to client)
- [x] Move httpx import to module level (fixed 2026-03-01)
- [x] Fix mutable default argument in `ingest_project_directory` (fixed 2026-03-01)
- [ ] Per-tenant rate limiting (optional)

## Performance

- [ ] Async batch parallelism with `asyncio.gather()`

## Refactoring

- [ ] [MED] Refactor `answer_query` — complexity 21 > 10 (ruff C901)
- [ ] [LOW] Consider splitting tools.py (1519 lines) into logical modules

## Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools
- [ ] Production config validation (fail fast on unsafe defaults)
