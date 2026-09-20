"""FastAPI application entry point.

Run with:  uvicorn app:app --reload --port 8011
           or: python app.py
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.v1.router import api_router
from core.config import settings
from db.session import engine

# Import for the side effect of registering every table on Base.metadata,
# which Alembic autogeneration reads.
import models  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Explicit origins, not "*": credentialed requests require it, and a
    # wildcard here would let any site call the API with a user's token.
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/api/v1/health", tags=["health"])
async def health() -> dict:
    """Liveness plus a real database round trip.

    A health check that does not touch its dependencies reports "ok" while the
    service is unable to serve a single request.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        database = f"error: {type(exc).__name__}"

    return {
        "status": "ok" if database == "ok" else "degraded",
        "environment": settings.ENVIRONMENT,
        "database": database,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, reload=True)
