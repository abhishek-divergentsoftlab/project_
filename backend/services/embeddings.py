"""Text embeddings via Ollama.

``nomic-embed-text`` was chosen by measurement, not by size. Against a cable
listing it scores a sibling cable at 0.986, a GaN charger at 0.577 and a
corrugated box at 0.418 -- a wide enough spread to filter on. The much larger
``qwen3-embedding:8b`` took ten times as long and rated the charger 0.767, too
close to a cable to separate them.

Every call is best-effort: if Ollama is unreachable the caller gets None and
falls back to lexical matching, because search must degrade rather than fail.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Optional, Sequence

from core.config import settings

logger = logging.getLogger(__name__)

def get_expected_dimensions(model_name: Optional[str] = None) -> int:
    model = model_name or settings.EMBEDDING_MODEL
    if "qwen3" in model or "qwen" in model:
        return 4096
    return 768


# Must match the model. Changing either means recreating the collection.
EMBEDDING_DIMENSIONS = get_expected_dimensions()


def _post(path: str, payload: dict, timeout: int) -> Optional[dict]:
    if path == "/api/embed" and settings.EMBEDDING_NUM_GPU >= 0:
        # On a unified-memory box (DGX Spark) CUDA reports only a few GB free,
        # so loading the embedding model on the GPU evicts the chat model and
        # the next chat message pays a ~45 s reload. num_gpu=0 keeps it on CPU.
        payload = {**payload, "options": {**payload.get("options", {}), "num_gpu": settings.EMBEDDING_NUM_GPU}}
    request = urllib.request.Request(
        f"{settings.OLLAMA_BASE_URL.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.warning("embedding call failed: %s", exc)
        return None


# Query-time embedding (a user is waiting) must never queue behind slow background indexing or exhaust the default
# executor: it has its own small pool and a short HTTP timeout. Background indexing keeps the long timeout.
QUERY_TIMEOUT_SECONDS = 3.0
_QUERY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed-query")


def _query_timeout() -> float:
    try:
        return min(QUERY_TIMEOUT_SECONDS, float(getattr(settings, "EMBEDDING_QUERY_TIMEOUT_SECONDS", QUERY_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        return QUERY_TIMEOUT_SECONDS


def _embed_sync(texts: Sequence[str], timeout: Optional[float] = None) -> Optional[list[list[float]]]:
    if not texts:
        return []

    expected_dim = get_expected_dimensions(settings.EMBEDDING_MODEL)
    data = _post(
        "/api/embed",
        {
            "model": settings.EMBEDDING_MODEL,
            "input": list(texts),
            "keep_alive": -1,
        },
        timeout if timeout is not None else settings.EMBEDDING_TIMEOUT_SECONDS,
    )
    if not data:
        return None

    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        logger.warning("embedding response did not match the number of inputs")
        return None
    if vectors and len(vectors[0]) != expected_dim:
        # A dimension mismatch would be rejected by the collection anyway, and
        # silently indexing half a corpus at the wrong width is worse.
        logger.warning(
            "embedding model returned %s dimensions, expected %s",
            len(vectors[0]), expected_dim,
        )
        return None
    return vectors


async def embed_many(texts: Sequence[str]) -> Optional[list[list[float]]]:
    """Embed a batch. None if the model is unreachable or the width is wrong."""
    return await asyncio.to_thread(_embed_sync, texts)


async def embed_one(text: str) -> Optional[list[float]]:
    vectors = await embed_many([text])
    return vectors[0] if vectors else None


async def embed_query(text: str) -> Optional[list[float]]:
    """Embed a search query: short HTTP timeout, dedicated bounded thread pool (a hung Ollama cannot exhaust the
    default executor). None when the model does not answer in time."""
    loop = asyncio.get_running_loop()
    vectors = await loop.run_in_executor(_QUERY_POOL, partial(_embed_sync, [text], _query_timeout()))
    return vectors[0] if vectors else None
