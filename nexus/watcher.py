# Version: v1.3
"""
nexus.watcher — Continuous RAG sync daemon.

Watches the antigravity workspace for changes to core documentation files
and automatically syncs them into RAG stores (Neo4j + Qdrant):

  - Content hash deduplication — skips unchanged files
  - Filepath deletion before re-ingest on updates (no stale chunks)
  - Stale document removal on file deletion
  - 3-second debounce to coalesce rapid saves (e.g. editor autosave)
  - Thread-safe event queuing from watchdog background thread

Usage:
    poetry run python -m nexus.watcher
    poetry run python -m nexus.watcher --workspace /home/turiya/antigravity --debounce 3.0
"""

import argparse
import asyncio
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from nexus.config import logger
from nexus.sync import (
    CORE_DOC_PATTERNS,
    PERSONA_FILES,
    PROJECT_MAPPINGS,
    _classify_file,
    check_file_changed,
)
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus import cache as cache_module

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/home/turiya/antigravity"))
DEBOUNCE_SECONDS = float(os.environ.get("RAG_SYNC_DEBOUNCE", "3.0"))


# ---------------------------------------------------------------------------
# Event handler (runs in watchdog background thread)
# ---------------------------------------------------------------------------


class CoreDocEventHandler(FileSystemEventHandler):
    """Queue file-change events for debounced async processing.

    Watchdog dispatches events from a background thread.  All state is
    protected by a threading.Lock so the async main loop can safely
    call ``pop_ready()`` without races.
    """

    def __init__(self, workspace_root: Path) -> None:
        super().__init__()
        self._workspace_root = workspace_root
        self._lock = threading.Lock()
        # abs_path_str -> monotonic timestamp of last event
        self._pending_changed: dict[str, float] = {}
        # abs_path_str of files deleted from disk
        self._pending_deleted: set[str] = set()

    # ---- watchdog callbacks ------------------------------------------------

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._queue_change(Path(event.src_path))

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._queue_change(Path(event.src_path))

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._queue_delete(Path(event.src_path))

    def on_moved(self, event) -> None:
        if not event.is_directory:
            # src gone, dst appeared
            self._queue_delete(Path(event.src_path))
            self._queue_change(Path(event.dest_path))

    # ---- internal ----------------------------------------------------------

    def _queue_change(self, filepath: Path) -> None:
        if not _classify_file(filepath, self._workspace_root):
            return
        with self._lock:
            self._pending_changed[str(filepath)] = time.monotonic()
            self._pending_deleted.discard(str(filepath))

    def _queue_delete(self, filepath: Path) -> None:
        if not _classify_file(filepath, self._workspace_root):
            return
        with self._lock:
            self._pending_deleted.add(str(filepath))
            self._pending_changed.pop(str(filepath), None)

    # ---- called from async main loop ---------------------------------------

    def pop_ready(self, debounce: float) -> tuple[list[str], list[str]]:
        """Return ``(changed, deleted)`` paths that have passed the debounce window.

        Thread-safe.  Removes returned entries from internal queues.
        """
        now = time.monotonic()
        with self._lock:
            ready_changed = [
                p for p, t in self._pending_changed.items() if now - t >= debounce
            ]
            for p in ready_changed:
                del self._pending_changed[p]

            ready_deleted = list(self._pending_deleted)
            self._pending_deleted.clear()

        return ready_changed, ready_deleted


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def _delete_from_rag(project_id: str, filepath_str: str, scope: str) -> None:
    """Remove all RAG documents tagged with *filepath_str* from both stores."""
    try:
        neo4j_backend.delete_by_filepath(project_id, filepath_str, scope)
    except Exception as e:
        logger.debug(f"Neo4j delete skip ({filepath_str}): {e}")
    try:
        qdrant_backend.delete_by_filepath(project_id, filepath_str, scope)
    except Exception as e:
        logger.debug(f"Qdrant delete skip ({filepath_str}): {e}")


async def _sync_changed(paths: list[str], workspace_root: Path) -> None:
    """For each path: delete old RAG chunks then ingest updated content."""
    from nexus.tools import ingest_graph_document, ingest_vector_document

    for abs_path_str in paths:
        filepath = Path(abs_path_str)
        if not filepath.exists():
            logger.warning(f"Watcher: file gone before sync: {abs_path_str}")
            continue

        classification = _classify_file(filepath, workspace_root)
        if not classification:
            continue
        project_id, scope = classification

        # Skip if content identical to what's already in RAG
        if not check_file_changed(filepath, project_id, scope):
            logger.debug(f"Watcher: unchanged, skipping {abs_path_str}")
            continue

        try:
            content = filepath.read_text(encoding="utf-8")
            try:
                source_id = str(filepath.relative_to(workspace_root))
            except ValueError:
                source_id = abs_path_str

            # Delete old chunks (by file_path metadata) before re-ingesting.
            # Invalidate cache immediately so stale results aren't served if
            # either ingest call below fails (fail-open: empty > stale).
            _delete_from_rag(project_id, abs_path_str, scope)
            cache_module.invalidate_cache(project_id, scope)

            graph_result = await ingest_graph_document(
                text=content,
                project_id=project_id,
                scope=scope,
                source_identifier=source_id,
                file_path=abs_path_str,
            )
            vector_result = await ingest_vector_document(
                text=content,
                project_id=project_id,
                scope=scope,
                source_identifier=source_id,
                file_path=abs_path_str,
            )

            # Bug L12-4 fix: use "Error" not in instead of "Successfully" in.
            # "Skipped: duplicate" is a valid non-error result (content already ingested
            # by a concurrent call) and should NOT trigger a WARNING.
            if "Error" not in graph_result and "Error" not in vector_result:
                logger.info(f"Watcher: synced {source_id} ({project_id}/{scope})")
            else:
                logger.warning(
                    f"Watcher: partial sync {source_id}: "
                    f"graph={graph_result[:80]!r}, vector={vector_result[:80]!r}"
                )
        except Exception as e:
            logger.error(f"Watcher: sync error for {abs_path_str}: {e}")


async def _sync_deleted(paths: list[str], workspace_root: Path) -> None:
    """Remove each deleted file's RAG documents from both stores."""
    for abs_path_str in paths:
        filepath = Path(abs_path_str)
        classification = _classify_file(filepath, workspace_root)
        if not classification:
            continue
        project_id, scope = classification
        _delete_from_rag(project_id, abs_path_str, scope)
        cache_module.invalidate_cache(project_id, scope)
        logger.info(f"Watcher: removed deleted file from RAG: {abs_path_str}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_watcher(
    workspace_root: Path = WORKSPACE_ROOT,
    debounce: float = DEBOUNCE_SECONDS,
) -> None:
    """Start the RAG sync watcher and block until interrupted.

    Args:
        workspace_root: Antigravity workspace root (watched recursively).
        debounce: Seconds to wait after the last file event before syncing.
    """
    handler = CoreDocEventHandler(workspace_root)
    observer = Observer()
    observer.schedule(handler, str(workspace_root), recursive=True)
    observer.start()

    logger.info(
        f"RAG sync watcher started | workspace={workspace_root} | debounce={debounce}s"
    )
    logger.info(
        f"Tracking: {len(PERSONA_FILES)} persona files + "
        f"{len(CORE_DOC_PATTERNS)} core-doc patterns × {len(PROJECT_MAPPINGS)} projects"
    )

    try:
        while True:
            await asyncio.sleep(1.0)
            changed, deleted = handler.pop_ready(debounce)
            if changed:
                logger.info(f"Watcher: {len(changed)} file(s) changed")
                await _sync_changed(changed, workspace_root)
            if deleted:
                logger.info(f"Watcher: {len(deleted)} file(s) deleted")
                await _sync_deleted(deleted, workspace_root)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("RAG sync watcher shutting down...")
    finally:
        observer.stop()
        observer.join()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nexus RAG Sync Watcher — continuously ingests core docs on change"
    )
    parser.add_argument(
        "--workspace",
        default=str(WORKSPACE_ROOT),
        help=f"Antigravity workspace root (default: {WORKSPACE_ROOT})",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=DEBOUNCE_SECONDS,
        help=f"Seconds after last file event before syncing (default: {DEBOUNCE_SECONDS})",
    )
    args = parser.parse_args()
    asyncio.run(
        run_watcher(workspace_root=Path(args.workspace), debounce=args.debounce)
    )


if __name__ == "__main__":
    main()
