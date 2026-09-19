"""Qdrant: the retrieval layer, never the source of truth.

Every point here can be rebuilt from PostgreSQL by ``scripts/index_vectors.py``.
Nothing is stored in the payload that is not needed to *filter* -- the full RFQ
is always read back from the database after retrieval.

The hard filters travel with the query as payload conditions, so the central
marketplace rule (a buyer only ever sees sellers) is enforced inside the vector
search rather than after it.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Optional, Sequence

from qdrant_client import AsyncQdrantClient, models

from core.config import settings
from services.embeddings import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

_client: Optional[AsyncQdrantClient] = None


def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=30)
    return _client


async def ensure_collection() -> bool:
    """Create the collection and its payload indexes if they are missing."""
    try:
        if not await client().collection_exists(settings.QDRANT_COLLECTION):
            await client().create_collection(
                collection_name=settings.QDRANT_COLLECTION,
                vectors_config=models.VectorParams(
                    size=EMBEDDING_DIMENSIONS, distance=models.Distance.COSINE
                ),
            )
        # Indexed payload fields make the hard filters cheap.
        for field, schema in (
            ("role", models.PayloadSchemaType.KEYWORD),
            ("status", models.PayloadSchemaType.KEYWORD),
            ("category", models.PayloadSchemaType.KEYWORD),
            ("user_id", models.PayloadSchemaType.KEYWORD),
            ("expires_at", models.PayloadSchemaType.INTEGER),
        ):
            try:
                await client().create_payload_index(
                    collection_name=settings.QDRANT_COLLECTION,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception:
                # Already indexed; Qdrant has no idempotent form of this call.
                pass
        return True
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        logger.warning("could not prepare the Qdrant collection: %s", exc)
        return False


def _timestamp(value: Optional[datetime]) -> int:
    """Expiry as a sortable integer; absent means "never", so use a far future."""
    if value is None:
        return 4102444800  # 2100-01-01
    return int(value.timestamp())


def build_payload(rfq: Any) -> dict:
    return {
        "rfq_id": str(rfq.id),
        "user_id": str(rfq.user_id),
        "role": rfq.role.value,
        "status": rfq.status.value,
        "category": rfq.category,
        "expires_at": _timestamp(rfq.expires_at),
    }


async def upsert(points: Sequence[tuple[uuid.UUID, list[float], dict]]) -> bool:
    if not points:
        return True
    try:
        await client().upsert(
            collection_name=settings.QDRANT_COLLECTION,
            points=[
                models.PointStruct(id=str(rfq_id), vector=vector, payload=payload)
                for rfq_id, vector, payload in points
            ],
            wait=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant upsert failed: %s", exc)
        return False


async def delete(rfq_ids: Sequence[uuid.UUID]) -> bool:
    if not rfq_ids:
        return True
    try:
        await client().delete(
            collection_name=settings.QDRANT_COLLECTION,
            points_selector=models.PointIdsList(points=[str(i) for i in rfq_ids]),
            wait=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant delete failed: %s", exc)
        return False


async def search(
    vector: list[float],
    *,
    target_role: str,
    exclude_user_id: uuid.UUID,
    limit: int,
    score_threshold: Optional[float] = None,
) -> Optional[list[tuple[uuid.UUID, float]]]:
    """Return (rfq_id, cosine score), or None when Qdrant is unavailable.

    The filters are the same non-negotiables the SQL path applies, expressed as
    payload conditions so they run inside the search.
    """
    now = int(datetime.now(UTC).timestamp())
    query_filter = models.Filter(
        must=[
            models.FieldCondition(key="role", match=models.MatchValue(value=target_role)),
            models.FieldCondition(key="status", match=models.MatchValue(value="active")),
            models.FieldCondition(key="expires_at", range=models.Range(gt=now)),
        ],
        must_not=[
            models.FieldCondition(
                key="user_id", match=models.MatchValue(value=str(exclude_user_id))
            )
        ],
    )

    try:
        response = await client().query_points(
            collection_name=settings.QDRANT_COLLECTION,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant search failed, falling back to SQL: %s", exc)
        return None

    return [(uuid.UUID(str(point.id)), float(point.score)) for point in response.points]


async def all_point_ids() -> Optional[set[uuid.UUID]]:
    """Every id currently in the collection, for reconciliation."""
    found: set[uuid.UUID] = set()
    offset = None
    try:
        while True:
            points, offset = await client().scroll(
                collection_name=settings.QDRANT_COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            found.update(uuid.UUID(str(point.id)) for point in points)
            if offset is None:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list Qdrant points: %s", exc)
        return None
    return found


async def count() -> Optional[int]:
    try:
        result = await client().count(settings.QDRANT_COLLECTION, exact=True)
        return result.count
    except Exception:  # noqa: BLE001
        return None
