"""Embedding providers.

* ``VoyageEmbedder``: voyage-code-3 (code-specialised), used in production.
* ``HashingEmbedder``: deterministic feature hashing over code-aware tokens. Needs no network or
  key, so dev, CI and the offline eval harness work everywhere. Lower quality, same interface.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol

import httpx

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class Embedder(Protocol):
    dimensions: int

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]: ...


def code_tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN.findall(text):
        out.append(tok.lower())
        parts = [p for part in tok.split("_") for p in _CAMEL.split(part) if p]
        if len(parts) > 1:
            out.extend(p.lower() for p in parts)
    return out


class HashingEmbedder:
    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        toks = code_tokens(text)
        grams = toks + [f"{a} {b}" for a, b in itertools.pairwise(toks)]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
            v[h % self.dimensions] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        return [self._vec(t) for t in texts]


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str = "voyage-code-3", dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self._model = model
        self._client = httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {api_key}"})

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            resp = await self._client.post(
                self.URL,
                json={
                    "model": self._model,
                    "input": [t[:30_000] for t in texts[i : i + 64]],
                    "input_type": "query" if kind == "query" else "document",
                    "output_dimension": self.dimensions,
                },
            )
            resp.raise_for_status()
            out += [d["embedding"] for d in sorted(resp.json()["data"], key=lambda d: d["index"])]
        return out


def make_embedder(provider: str, *, api_key: str | None, model: str, dimensions: int) -> Embedder:
    if provider == "voyage":
        if not api_key:
            raise ValueError("EMBEDDINGS__VOYAGE_API_KEY is required for the voyage provider")
        return VoyageEmbedder(api_key, model, dimensions)
    return HashingEmbedder(dimensions)
