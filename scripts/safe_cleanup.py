#!/usr/bin/env python3
# Version: v1.4
"""Safe integrity cleanup for Nexus RAG + Code-Graph-RAG backends.

Default mode is dry-run. Use --apply to perform deletions.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from neo4j import GraphDatabase
from qdrant_client import QdrantClient

from nexus.config import (
    COLLECTION_NAME,
    DEFAULT_NEO4J_PASSWORD,
    DEFAULT_NEO4J_URL,
    DEFAULT_NEO4J_USER,
    DEFAULT_QDRANT_URL,
)


@dataclass
class CleanupStats:
    neo_dup_groups: int = 0
    neo_dup_nodes: int = 0
    neo_unscoped_chunks: int = 0
    neo_deleted_dup_nodes: int = 0
    neo_deleted_unscoped: int = 0
    qdr_dup_groups: int = 0
    qdr_dup_nodes: int = 0
    qdr_deleted_dup_nodes: int = 0
    neo_abs_file_paths: int = 0
    neo_normalized_paths: int = 0
    qdr_abs_file_paths: int = 0
    qdr_normalized_paths: int = 0
    mem_total_files: int = 0
    mem_stale_or_unwanted: int = 0
    mem_deleted_nodes: int = 0


def _is_unwanted_memgraph_path(path: str) -> bool:
    p = Path(path)
    name = p.name
    if ".playwright-mcp" in path and path.endswith(".log"):
        return True
    if name == ".coverage":
        return True
    # GNU sed temporary files often look like sedA1b2C
    if re.fullmatch(r"sed[0-9A-Za-z]{5}", name):
        return True
    return False


def audit_and_cleanup_neo4j(apply: bool, stats: CleanupStats) -> None:
    driver = GraphDatabase.driver(
        DEFAULT_NEO4J_URL, auth=(DEFAULT_NEO4J_USER, DEFAULT_NEO4J_PASSWORD)
    )
    with driver.session() as s:
        dup = s.run(
            """
            MATCH (n:Chunk)
            WHERE n.project_id IS NOT NULL AND n.tenant_scope IS NOT NULL AND n.content_hash IS NOT NULL
            WITH n.project_id AS pid, n.tenant_scope AS scope, n.content_hash AS h, count(*) AS c
            WHERE c > 1
            RETURN count(*) AS groups, coalesce(sum(c - 1), 0) AS nodes
            """
        ).single()
        stats.neo_dup_groups = int(dup["groups"])
        stats.neo_dup_nodes = int(dup["nodes"])

        unscoped = s.run(
            """
            MATCH (n:Chunk)
            WHERE n.project_id IS NULL OR n.tenant_scope IS NULL OR trim(toString(n.project_id)) = '' OR trim(toString(n.tenant_scope)) = ''
            RETURN count(n) AS c
            """
        ).single()
        stats.neo_unscoped_chunks = int(unscoped["c"])

        abs_fp = s.run(
            """
            MATCH (n)
            WHERE n.file_path IS NOT NULL AND toString(n.file_path) STARTS WITH '/home/turiya/antigravity/'
            RETURN count(n) AS c
            """
        ).single()
        stats.neo_abs_file_paths = int(abs_fp["c"])

        if apply:
            dedup_res = s.run(
                """
                MATCH (n:Chunk)
                WHERE n.project_id IS NOT NULL AND n.tenant_scope IS NOT NULL AND n.content_hash IS NOT NULL
                WITH n.project_id AS pid, n.tenant_scope AS scope, n.content_hash AS h, collect(n) AS nodes
                WHERE size(nodes) > 1
                WITH nodes, size(nodes) - 1 AS to_delete
                FOREACH (n IN nodes[1..] | DETACH DELETE n)
                RETURN count(*) AS groups, coalesce(sum(to_delete), 0) AS deleted
                """
            ).single()
            stats.neo_deleted_dup_nodes = int(dedup_res["deleted"])

            unscoped_res = s.run(
                """
                MATCH (n:Chunk)
                WHERE n.project_id IS NULL OR n.tenant_scope IS NULL OR trim(toString(n.project_id)) = '' OR trim(toString(n.tenant_scope)) = ''
                WITH collect(n) AS nodes, count(n) AS c
                FOREACH (n IN nodes | DETACH DELETE n)
                RETURN c AS deleted
                """
            ).single()
            stats.neo_deleted_unscoped = int(unscoped_res["deleted"])

            normalized = s.run(
                """
                MATCH (n)
                WHERE n.file_path IS NOT NULL AND toString(n.file_path) STARTS WITH '/home/turiya/antigravity/'
                WITH n, substring(toString(n.file_path), size('/home/turiya/antigravity/') ) AS rel
                SET n.file_path = rel
                RETURN count(n) AS c
                """
            ).single()
            stats.neo_normalized_paths = int(normalized["c"])

    driver.close()


def audit_and_cleanup_qdrant(apply: bool, stats: CleanupStats) -> None:
    client = QdrantClient(url=DEFAULT_QDRANT_URL)

    by_key: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    normalize_updates: list[tuple[str, str]] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break

        for p in points:
            pl = p.payload or {}
            pid = pl.get("project_id")
            scope = pl.get("tenant_scope")
            ch = pl.get("content_hash")
            fp = pl.get("file_path")
            if pid and scope and ch:
                by_key[(str(pid), str(scope), str(ch))].append(str(p.id))
            if isinstance(fp, str) and fp.startswith("/home/turiya/antigravity/"):
                stats.qdr_abs_file_paths += 1
                normalize_updates.append(
                    (str(p.id), fp.removeprefix("/home/turiya/antigravity/"))
                )

        if offset is None:
            break

    delete_ids: list[str] = []
    for ids in by_key.values():
        if len(ids) > 1:
            stats.qdr_dup_groups += 1
            stats.qdr_dup_nodes += len(ids) - 1
            delete_ids.extend(ids[1:])

    if apply and delete_ids:
        client.delete(collection_name=COLLECTION_NAME, points_selector=delete_ids)
        stats.qdr_deleted_dup_nodes = len(delete_ids)

    if apply and normalize_updates:
        # Payload updates are done point-by-point to avoid large in-memory batches.
        for point_id, rel_path in normalize_updates:
            client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"file_path": rel_path},
                points=[point_id],
            )
        stats.qdr_normalized_paths = len(normalize_updates)


def audit_and_cleanup_memgraph(apply: bool, stats: CleanupStats) -> None:
    # Use the Neo4j Bolt driver for Memgraph so cleanup works in the default
    # Poetry environment without an extra mgclient dependency.
    root = Path("/home/turiya/antigravity")
    driver = GraphDatabase.driver("bolt://localhost:7688", auth=None)

    with driver.session() as session:
        rows = session.run("MATCH (f:File) RETURN f.path AS path")
        paths = [r["path"] for r in rows if r and r.get("path")]
        stats.mem_total_files = len(paths)

        targets: list[str] = []
        for path in paths:
            full = root / path
            if (not full.exists()) or _is_unwanted_memgraph_path(path):
                targets.append(path)

        stats.mem_stale_or_unwanted = len(targets)

        if apply and targets:
            deleted = 0
            for path in targets:
                file_row = session.run(
                    "MATCH (f:File {path: $path}) RETURN count(f) AS c", path=path
                ).single()
                mod_row = session.run(
                    "MATCH (m:Module {path: $path}) RETURN count(m) AS c", path=path
                ).single()
                deleted += int(file_row["c"]) if file_row else 0
                deleted += int(mod_row["c"]) if mod_row else 0

                session.run("MATCH (f:File {path: $path}) DETACH DELETE f", path=path)
                session.run("MATCH (m:Module {path: $path}) DETACH DELETE m", path=path)
            stats.mem_deleted_nodes = deleted

    driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safe cleanup for RAG/Graph data integrity"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply deletions (default is dry-run)"
    )
    args = parser.parse_args()

    stats = CleanupStats()
    audit_and_cleanup_neo4j(args.apply, stats)
    audit_and_cleanup_qdrant(args.apply, stats)
    audit_and_cleanup_memgraph(args.apply, stats)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"SAFE CLEANUP REPORT [{mode}]")
    print(f"Neo4j duplicate hash groups: {stats.neo_dup_groups}")
    print(f"Neo4j duplicate nodes (extra): {stats.neo_dup_nodes}")
    print(f"Neo4j unscoped chunks: {stats.neo_unscoped_chunks}")
    print(f"Neo4j deleted duplicate nodes: {stats.neo_deleted_dup_nodes}")
    print(f"Neo4j deleted unscoped chunks: {stats.neo_deleted_unscoped}")
    print(f"Qdrant duplicate hash groups: {stats.qdr_dup_groups}")
    print(f"Qdrant duplicate points (extra): {stats.qdr_dup_nodes}")
    print(f"Qdrant deleted duplicate points: {stats.qdr_deleted_dup_nodes}")
    print(f"Neo4j absolute file_path nodes: {stats.neo_abs_file_paths}")
    print(f"Neo4j normalized file_path nodes: {stats.neo_normalized_paths}")
    print(f"Qdrant absolute file_path points: {stats.qdr_abs_file_paths}")
    print(f"Qdrant normalized file_path points: {stats.qdr_normalized_paths}")
    print(f"Memgraph File nodes: {stats.mem_total_files}")
    print(f"Memgraph stale/unwanted targets: {stats.mem_stale_or_unwanted}")
    print(f"Memgraph deleted nodes (best-effort rowcount): {stats.mem_deleted_nodes}")


if __name__ == "__main__":
    main()
