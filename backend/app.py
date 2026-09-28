"""FastAPI application entry point.

Run with:  uvicorn app:app --reload --port 8011
           or: python app.py
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.v1.router import api_router
from core.config import settings
from core.logging_setup import configure_logging
from core.security_headers import SecurityHeadersMiddleware
from db.session import SessionLocal, engine
from services.scheduler import Scheduler, scheduler_disabled

# Import for the side effect of registering every table on Base.metadata,
# which Alembic autogeneration reads.
import models  # noqa: F401

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Here rather than at import time: uvicorn has configured its own loggers
    # by now (so the access-log redaction filter lands on the real ones), and
    # importing the app in tests does not rewire pytest's logging.
    configure_logging()

    scheduler: Scheduler | None = None
    if scheduler_disabled():
        logger.info("background scheduler disabled")
    else:
        scheduler = Scheduler(SessionLocal)
        scheduler.start()
    app.state.scheduler = scheduler

    logger.info("%s started (environment=%s)", settings.PROJECT_NAME, settings.ENVIRONMENT)
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    lifespan=lifespan,
)

# Added before CORS so CORS stays the outermost layer (Starlette runs the
# last-added middleware first); both only decorate the response.
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    # Explicit origins, not "*": credentialed requests require it, and a
    # wildcard here would let any site call the API with a user's token.
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _sanitize_for_json(obj):
    import math
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return str(obj)
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    errors = _sanitize_for_json(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": errors})

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

    configure_logging()
    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, reload=True)
