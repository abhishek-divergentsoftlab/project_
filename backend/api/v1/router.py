"""Aggregates the v1 routers."""

from fastapi import APIRouter

from api.v1 import auth, connections, matches, rfqs, search, users

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(rfqs.router, prefix="/rfqs", tags=["rfqs"])
# Mounted without a prefix: its paths are nested under /rfqs/{id} already.
api_router.include_router(connections.router)
api_router.include_router(matches.router)
api_router.include_router(search.router)
