"""Aggregates the v1 routers."""

from fastapi import APIRouter

from api.v1 import (
    ai_chat,
    auth,
    certificates,
    connections,
    currency,
    dashboard,
    escrow,
    logistics,
    marketplace,
    matches,
    media,
    moderation,
    notifications,
    quotations,
    reviews,
    rfqs,
    translation,
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
api_router.include_router(currency.router)
api_router.include_router(media.router)
api_router.include_router(websockets.router)
api_router.include_router(ai_chat.router)
api_router.include_router(dashboard.router)
api_router.include_router(notifications.router)
api_router.include_router(marketplace.router)
api_router.include_router(translation.router)
api_router.include_router(escrow.router)
api_router.include_router(logistics.router)



