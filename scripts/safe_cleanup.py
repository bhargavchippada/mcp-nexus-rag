#!/usr/bin/env python3
# Version: v2.0
"""Safe integrity cleanup for Nexus RAG + Code-Graph-RAG backends.

Default mode is dry-run. Use --apply to perform deletions.

Backends:
  - Memgraph RAG (port 7689): GraphRAG property graph store
  - pgvector (Postgres): Vector store with metadata in JSONB
  - Memgraph CGR (port 7688): Code-Graph-RAG AST index
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

from nexus.config import (
    DEFAULT_MEMGRAPH_URL,
    DEFAULT_PG_DB,
    DEFAULT_PG_HOST,
    DEFAULT_PG_PASSWORD,
    DEFAULT_PG_PORT,
    DEFAULT_PG_USER,
    PG_TABLE_NAME_SQL,
)


@dataclass
class CleanupStats:
    graph_dup_groups: int = 0
    graph_dup_nodes: int = 0
    graph_unscoped_chunks: int = 0
    graph_deleted_dup_nodes: int = 0
    graph_deleted_unscoped: int = 0
    pgv_dup_groups: int = 0
    pgv_dup_nodes: int = 0
    pgv_deleted_dup_nodes: int = 0
    graph_abs_file_paths: int = 0
    graph_normalized_paths: int = 0
    pgv_abs_file_paths: int = 0
    pgv_normalized_paths: int = 0
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


def audit_and_cleanup_memgraph_rag(apply: bool, stats: CleanupStats) -> None:
    """Audit and clean the Memgraph RAG graph store (port 7689)."""
    driver = GraphDatabase.driver(DEFAULT_MEMGRAPH_URL, auth=("", ""))
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
        stats.graph_dup_groups = int(dup["groups"])
        stats.graph_dup_nodes = int(dup["nodes"])

        unscoped = s.run(
            """
            MATCH (n:Chunk)
            WHERE n.project_id IS NULL OR n.tenant_scope IS NULL OR trim(toString(n.project_id)) = '' OR trim(toString(n.tenant_scope)) = ''
            RETURN count(n) AS c
            """
        ).single()
        stats.graph_unscoped_chunks = int(unscoped["c"])

        abs_fp = s.run(
            """
            MATCH (n)
            WHERE n.file_path IS NOT NULL AND toString(n.file_path) STARTS WITH '/home/turiya/antigravity/'
            RETURN count(n) AS c
            """
        ).single()
        stats.graph_abs_file_paths = int(abs_fp["c"])

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
            stats.graph_deleted_dup_nodes = int(dedup_res["deleted"])

            unscoped_res = s.run(
                """
                MATCH (n:Chunk)
                WHERE n.project_id IS NULL OR n.tenant_scope IS NULL OR trim(toString(n.project_id)) = '' OR trim(toString(n.tenant_scope)) = ''
                WITH collect(n) AS nodes, count(n) AS c
                FOREACH (n IN nodes | DETACH DELETE n)
                RETURN c AS deleted
                """
            ).single()
            stats.graph_deleted_unscoped = int(unscoped_res["deleted"])

            normalized = s.run(
                """
                MATCH (n)
                WHERE n.file_path IS NOT NULL AND toString(n.file_path) STARTS WITH '/home/turiya/antigravity/'
                WITH n, substring(toString(n.file_path), size('/home/turiya/antigravity/') ) AS rel
                SET n.file_path = rel
                RETURN count(n) AS c
                """
            ).single()
            stats.graph_normalized_paths = int(normalized["c"])

    driver.close()


def audit_and_cleanup_pgvector(apply: bool, stats: CleanupStats) -> None:
    """Audit and clean the pgvector store."""
    conn = psycopg2.connect(
        host=DEFAULT_PG_HOST,
        port=DEFAULT_PG_PORT,
        dbname=DEFAULT_PG_DB,
        user=DEFAULT_PG_USER,
        password=DEFAULT_PG_PASSWORD,
    )
    conn.autocommit = True
    cur = conn.cursor()
    table = PG_TABLE_NAME_SQL

    # Find duplicate content_hash groups
    cur.execute(
        f"""
        SELECT metadata_->>'project_id', metadata_->>'tenant_scope',
               metadata_->>'content_hash', count(*) AS c
        FROM {table}
        WHERE metadata_->>'content_hash' IS NOT NULL
          AND metadata_->>'project_id' IS NOT NULL
          AND metadata_->>'tenant_scope' IS NOT NULL
        GROUP BY metadata_->>'project_id', metadata_->>'tenant_scope',
                 metadata_->>'content_hash'
        HAVING count(*) > 1
        """
    )
    dup_rows = cur.fetchall()
    stats.pgv_dup_groups = len(dup_rows)
    stats.pgv_dup_nodes = sum(r[3] - 1 for r in dup_rows)

    # Find absolute file paths
    cur.execute(
        f"""
        SELECT count(*) FROM {table}
        WHERE metadata_->>'file_path' LIKE '/home/turiya/antigravity/%%'
        """
    )
    stats.pgv_abs_file_paths = cur.fetchone()[0]

    if apply:
        # Delete duplicate rows (keep one per group)
        deleted = 0
        for pid, scope, ch, cnt in dup_rows:
            cur.execute(
                f"""
                DELETE FROM {table}
                WHERE id IN (
                    SELECT id FROM {table}
                    WHERE metadata_->>'project_id' = %s
                      AND metadata_->>'tenant_scope' = %s
                      AND metadata_->>'content_hash' = %s
                    ORDER BY id
                    OFFSET 1
                )
                """,
                (pid, scope, ch),
            )
            deleted += cur.rowcount
        stats.pgv_deleted_dup_nodes = deleted

        # Normalize absolute file paths
        cur.execute(
            f"""
            UPDATE {table}
            SET metadata_ = jsonb_set(
                metadata_,
                '{{file_path}}',
                to_jsonb(regexp_replace(metadata_->>'file_path', '^/home/turiya/antigravity/', ''))
            )
            WHERE metadata_->>'file_path' LIKE '/home/turiya/antigravity/%%'
            """
        )
        stats.pgv_normalized_paths = cur.rowcount

    cur.close()
    conn.close()


def audit_and_cleanup_memgraph_cgr(apply: bool, stats: CleanupStats) -> None:
    """Audit and clean the Code-Graph-RAG Memgraph instance (port 7688)."""
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
    audit_and_cleanup_memgraph_rag(args.apply, stats)
    audit_and_cleanup_pgvector(args.apply, stats)
    audit_and_cleanup_memgraph_cgr(args.apply, stats)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"SAFE CLEANUP REPORT [{mode}]")
    print(f"Memgraph RAG duplicate hash groups: {stats.graph_dup_groups}")
    print(f"Memgraph RAG duplicate nodes (extra): {stats.graph_dup_nodes}")
    print(f"Memgraph RAG unscoped chunks: {stats.graph_unscoped_chunks}")
    print(f"Memgraph RAG deleted duplicate nodes: {stats.graph_deleted_dup_nodes}")
    print(f"Memgraph RAG deleted unscoped chunks: {stats.graph_deleted_unscoped}")
    print(f"pgvector duplicate hash groups: {stats.pgv_dup_groups}")
    print(f"pgvector duplicate points (extra): {stats.pgv_dup_nodes}")
    print(f"pgvector deleted duplicate points: {stats.pgv_deleted_dup_nodes}")
    print(f"Memgraph RAG absolute file_path nodes: {stats.graph_abs_file_paths}")
    print(f"Memgraph RAG normalized file_path nodes: {stats.graph_normalized_paths}")
    print(f"pgvector absolute file_path points: {stats.pgv_abs_file_paths}")
    print(f"pgvector normalized file_path points: {stats.pgv_normalized_paths}")
    print(f"Memgraph CGR File nodes: {stats.mem_total_files}")
    print(f"Memgraph CGR stale/unwanted targets: {stats.mem_stale_or_unwanted}")
    print(
        f"Memgraph CGR deleted nodes (best-effort rowcount): {stats.mem_deleted_nodes}"
    )


if __name__ == "__main__":
    main()
