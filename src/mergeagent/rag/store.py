"""Code index: incremental indexing + hybrid (vector + full-text) retrieval with RRF.

Two implementations share one interface:
* ``PgVectorStore``: production, PostgreSQL + pgvector (HNSW cosine) + GIN tsvector.
* ``MemoryVectorStore``: in-process, for the CLI/eval harness when no database is configured.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mergeagent.db.models import CodeChunk
from mergeagent.rag.chunker import Chunk, chunk_file, should_index
from mergeagent.rag.embeddings import Embedder, code_tokens

RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    score: float


class CodeStore(Protocol):
    async def index_files(self, repo: str, files: dict[str, str]) -> int: ...
    async def search(self, repo: str, query: str, k: int = 8) -> list[SearchHit]: ...


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal-rank fusion of several ranked id lists."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _chunks(files: dict[str, str]) -> list[Chunk]:
    out: list[Chunk] = []
    for path, text in files.items():
        if should_index(path, len(text)):
            out += chunk_file(path, text)
    return out


class PgVectorStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._factory = factory
        self._embedder = embedder

    async def index_files(self, repo: str, files: dict[str, str]) -> int:
        chunks = _chunks(files)
        async with self._factory() as s, s.begin():
            existing = set(
                (await s.execute(
                    select(CodeChunk.content_hash).where(CodeChunk.repo_full_name == repo)
                )).scalars()
            )  # fmt: skip
            wanted = {c.content_hash for c in chunks}
            # Drop chunks for files that changed or disappeared.
            stale = existing - wanted
            if stale:
                await s.execute(
                    delete(CodeChunk).where(
                        CodeChunk.repo_full_name == repo, CodeChunk.content_hash.in_(stale)
                    )
                )
            new = [c for c in chunks if c.content_hash not in existing]
            if new:
                vectors = await self._embedder.embed([c.content for c in new])
                rows = [
                    {
                        "repo_full_name": repo, "path": c.path, "symbol": c.symbol,
                        "start_line": c.start_line, "end_line": c.end_line, "content": c.content,
                        "content_hash": c.content_hash, "embedding": v,
                    }
                    for c, v in zip(new, vectors, strict=True)
                ]  # fmt: skip
                for i in range(0, len(rows), 500):
                    await s.execute(
                        insert(CodeChunk).values(rows[i : i + 500]).on_conflict_do_nothing()
                    )
        return len(new)

    async def search(self, repo: str, query: str, k: int = 8) -> list[SearchHit]:
        [qvec] = await self._embedder.embed([query], kind="query")
        tsquery = func.plainto_tsquery("simple", " ".join(code_tokens(query)[:30]))
        async with self._factory() as s:
            vec_ids = list(
                (await s.execute(
                    select(CodeChunk.id).where(CodeChunk.repo_full_name == repo)
                    .order_by(CodeChunk.embedding.cosine_distance(qvec)).limit(k * 4)
                )).scalars()
            )  # fmt: skip
            fts_ids = list(
                (await s.execute(
                    select(CodeChunk.id).where(
                        CodeChunk.repo_full_name == repo, CodeChunk.tsv.op("@@")(tsquery)
                    ).order_by(func.ts_rank_cd(CodeChunk.tsv, tsquery).desc()).limit(k * 4)
                )).scalars()
            )  # fmt: skip
            fused = sorted(rrf([vec_ids, fts_ids]).items(), key=lambda kv: -kv[1])[:k]
            if not fused:
                return []
            rows = {
                r.id: r
                for r in (await s.execute(
                    select(CodeChunk).where(CodeChunk.id.in_([i for i, _ in fused]))
                )).scalars()
            }  # fmt: skip
        return [
            SearchHit(r.path, r.symbol, r.start_line, r.end_line, r.content, score)
            for i, score in fused
            if (r := rows.get(i)) is not None
        ]


class MemoryVectorStore:
    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._rows: dict[str, list[tuple[Chunk, list[float], set[str]]]] = {}

    async def index_files(self, repo: str, files: dict[str, str]) -> int:
        chunks = _chunks(files)
        vectors = await self._embedder.embed([c.content for c in chunks]) if chunks else []
        self._rows[repo] = [
            (c, v, set(code_tokens(c.content))) for c, v in zip(chunks, vectors, strict=True)
        ]
        return len(chunks)

    async def search(self, repo: str, query: str, k: int = 8) -> list[SearchHit]:
        rows = self._rows.get(repo, [])
        if not rows:
            return []
        [q] = await self._embedder.embed([query], kind="query")
        qtok = set(code_tokens(query))
        by_vec = sorted(
            range(len(rows)), key=lambda i: -sum(a * b for a, b in zip(q, rows[i][1], strict=True))
        )
        lexical = [(i, len(qtok & rows[i][2])) for i in range(len(rows))]
        by_lex = [i for i, n in sorted(lexical, key=lambda t: -t[1]) if n > 0]
        fused = sorted(rrf([by_vec[: k * 4], by_lex[: k * 4]]).items(), key=lambda kv: -kv[1])[:k]
        return [
            SearchHit(rows[i][0].path, rows[i][0].symbol, rows[i][0].start_line,
                      rows[i][0].end_line, rows[i][0].content, s)
            for i, s in fused
        ]  # fmt: skip


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)
