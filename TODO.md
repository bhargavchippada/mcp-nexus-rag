# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

## Hardening

- [ ] Exception message sanitization (return generic errors to client)
- [ ] Move httpx import to module level
- [ ] Fix mutable default argument in `ingest_project_directory`
- [ ] Per-tenant rate limiting (optional)

## Performance

- [ ] Async batch parallelism with `asyncio.gather()`

## Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools
- [ ] Production config validation (fail fast on unsafe defaults)
