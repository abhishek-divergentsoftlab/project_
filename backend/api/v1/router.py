"""Aggregates the v1 routers."""

from fastapi import APIRouter

from api.v1 import (
    auth,
    certificates,
    connections,
    currency,
    matches,
    media,
    moderation,
    quotations,
    reviews,
    rfqs,
    search,
    users,
    websockets,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(rfqs.router, prefix="/rfqs", tags=["rfqs"])
# Mounted without a prefix: its paths are nested under /rfqs/{id} already.
api_router.include_router(connections.router)
api_router.include_router(quotations.router)
api_router.include_router(reviews.router)
api_router.include_router(certificates.router, prefix="/certifications", tags=["certifications"])
api_router.include_router(moderation.router, prefix="/moderation", tags=["moderation"])
api_router.include_router(matches.router)
api_router.include_router(search.router)
api_router.include_router(currency.router)
api_router.include_router(media.router)
api_router.include_router(websockets.router)

