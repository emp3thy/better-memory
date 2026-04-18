"""Integration tests for :mod:`better_memory.embeddings.ollama`.

These tests hit a real Ollama server at ``$OLLAMA_HOST`` (default
``http://localhost:11434``) and require the ``nomic-embed-text`` model to be
pulled. The whole module is skipped automatically when Ollama is not
reachable, so the default ``pytest`` run (even without ``-m`` filtering) stays
green on machines that don't have Ollama running.

Run explicitly with::

    uv run pytest -m integration
"""

from __future__ import annotations

import math

import httpx
import pytest

from better_memory.config import get_config
from better_memory.embeddings.ollama import OllamaEmbedder

pytestmark = pytest.mark.integration


def _ollama_reachable() -> bool:
    host = get_config().ollama_host
    try:
        resp = httpx.get(f"{host}/api/tags", timeout=1.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


# Skip the whole module if Ollama isn't reachable, even when ``-m integration``
# is passed. This keeps CI runs sane and lets developers without Ollama use the
# marker without hitting confusing connection errors.
if not _ollama_reachable():
    pytest.skip("Ollama not reachable on configured OLLAMA_HOST", allow_module_level=True)


async def test_embed_returns_768_floats() -> None:
    async with OllamaEmbedder() as embedder:
        vec = await embedder.embed("hello world")

    assert isinstance(vec, list)
    assert len(vec) == 768
    assert all(isinstance(x, float) for x in vec)


async def test_embed_batch_returns_three_768_vectors() -> None:
    async with OllamaEmbedder() as embedder:
        batch = await embedder.embed_batch(["alpha", "beta", "gamma"])

    assert len(batch) == 3
    for vec in batch:
        assert len(vec) == 768
        assert all(isinstance(x, float) for x in vec)


async def test_embed_single_matches_batch_result_for_same_text() -> None:
    async with OllamaEmbedder() as embedder:
        single = await embedder.embed("alpha")
        batch = await embedder.embed_batch(["alpha"])

    assert len(batch) == 1
    assert len(single) == len(batch[0]) == 768
    for a, b in zip(single, batch[0], strict=True):
        assert math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6), (a, b)
