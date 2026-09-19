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
from typing import Optional, Sequence

from core.config import settings

logger = logging.getLogger(__name__)

# Must match the model. Changing either means recreating the collection.
EMBEDDING_DIMENSIONS = 768


def _post(path: str, payload: dict, timeout: int) -> Optional[dict]:
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


def _embed_sync(texts: Sequence[str]) -> Optional[list[list[float]]]:
    if not texts:
        return []

    data = _post(
        "/api/embed",
        {"model": settings.EMBEDDING_MODEL, "input": list(texts)},
        settings.EMBEDDING_TIMEOUT_SECONDS,
    )
    if not data:
        return None

    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        logger.warning("embedding response did not match the number of inputs")
        return None
    if vectors and len(vectors[0]) != EMBEDDING_DIMENSIONS:
        # A dimension mismatch would be rejected by the collection anyway, and
        # silently indexing half a corpus at the wrong width is worse.
        logger.warning(
            "embedding model returned %s dimensions, expected %s",
            len(vectors[0]), EMBEDDING_DIMENSIONS,
        )
        return None
    return vectors


async def embed_many(texts: Sequence[str]) -> Optional[list[list[float]]]:
    """Embed a batch. None if the model is unreachable or the width is wrong."""
    return await asyncio.to_thread(_embed_sync, texts)


async def embed_one(text: str) -> Optional[list[float]]:
    vectors = await embed_many([text])
    return vectors[0] if vectors else None
