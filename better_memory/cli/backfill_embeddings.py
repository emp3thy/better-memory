"""One-shot embedding backfill for reflections + semantic memories.

Run at deploy after migration 0014:

    python -m better_memory.cli.backfill_embeddings

Idempotent: only rows missing a vector are embedded. The lazy self-heal in
memory.retrieve covers stragglers afterwards; this exists so the historical
corpus doesn't wait to be retrieved before becoming searchable.

One event loop and one embedder for the whole job (the embedder's
httpx.AsyncClient is loop-bound); batches of 50 per HTTP request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import sqlite_vec

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import _embedding_source_text

_BATCH = 50


def backfill(conn, embedder) -> dict[str, int]:
    stats = {"reflections": 0, "semantics": 0, "skipped": 0}

    refl = conn.execute(
        """SELECT r.id, r.title, r.use_cases, r.hints FROM reflections r
           WHERE r.status IN ('pending_review', 'confirmed')
             AND r.id NOT IN (SELECT reflection_id FROM reflection_embeddings)"""
    ).fetchall()
    sems = conn.execute(
        """SELECT id, content FROM semantic_memories
           WHERE id NOT IN (SELECT memory_id FROM semantic_embeddings)"""
    ).fetchall()

    jobs = [
        ("reflections", "reflection_embeddings", "reflection_id", r["id"],
         _embedding_source_text(r["title"], r["use_cases"],
                                json.loads(r["hints"])))
        for r in refl
    ] + [
        ("semantics", "semantic_embeddings", "memory_id", s["id"], s["content"])
        for s in sems
    ]

    async def _embed_all() -> list[list[list[float]] | None]:
        out = []
        for i in range(0, len(jobs), _BATCH):
            texts = [j[4] for j in jobs[i:i + _BATCH]]
            try:
                out.append(await embedder.embed_batch(texts))
            except Exception:
                out.append(None)
        return out

    batches = asyncio.run(_embed_all()) if jobs else []

    for bi, vectors in enumerate(batches):
        chunk = jobs[bi * _BATCH:(bi + 1) * _BATCH]
        if vectors is None:
            stats["skipped"] += len(chunk)
            continue
        for (kind, table, col, row_id, _), vec in zip(chunk, vectors):
            conn.execute(
                f"INSERT INTO {table} ({col}, embedding) VALUES (?, ?)",
                (row_id, sqlite_vec.serialize_float32(vec)))
            stats[kind] += 1
    conn.commit()
    return stats


def main(argv: list[str] | None = None) -> None:
    from better_memory.config import get_config
    from better_memory.embeddings.ollama import OllamaEmbedder

    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None,
                    help="BETTER_MEMORY_HOME override (default: config)")
    args = ap.parse_args(argv)

    config = get_config()
    db = Path(args.home) / "memory.db" if args.home else config.memory_db
    conn = connect(db)
    apply_migrations(conn)

    if config.embeddings_backend != "ollama":
        print("embeddings backend is not ollama; nothing to backfill")
        return

    embedder = OllamaEmbedder()
    try:
        stats = backfill(conn, embedder)
    finally:
        asyncio.run(embedder.aclose())
    print(f"backfilled reflections={stats['reflections']} "
          f"semantics={stats['semantics']} skipped={stats['skipped']}")
    if stats["skipped"]:
        print("warning: some rows skipped (Ollama unreachable?); "
              "re-run later or let retrieve self-heal them", file=sys.stderr)


if __name__ == "__main__":
    main()
