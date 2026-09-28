"""Media streaming and asset serving router.

Access rules
------------
* ``live_captures/`` (deal-room photos) are private. They are served only with
  a valid HMAC signature from ``core.media_signing`` (``?exp=&sig=``, what the
  API puts in ``image_url``) or with a bearer access token belonging to a party
  of the connection the photo was posted in.
* ``certificates/`` stay public: they back the certificates shown on public
  seller profiles (``GET /certifications/user/{id}``) and are opened by the
  frontend in an ``<iframe>`` / ``<img>``, which cannot send a token.
* Only a fixed set of image/PDF types is served inline; anything else is sent
  as an ``attachment`` so a planted ``.html`` or ``.svg`` can never execute in
  the API's origin. Every response carries ``X-Content-Type-Options: nosniff``.
"""

import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import or_, select

from api.deps import DbSession, bearer_scheme
from core.config import settings
from core.media_signing import MEDIA_URL_PREFIX, PRIVATE_MEDIA_DIRS, verify_media_signature
from core.security import TokenError, decode_token

router = APIRouter(prefix="/media", tags=["media"])

INLINE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


async def _bearer_may_view(
    db: DbSession, credentials: Optional[HTTPAuthorizationCredentials], relpath: str
) -> bool:
    """True when the bearer token belongs to a party of the connection that holds this image."""
    if credentials is None:
        return False
    try:
        user_id = uuid.UUID(decode_token(credentials.credentials, expected_type="access")["sub"])
    except (TokenError, ValueError, KeyError):
        return False

    from models.connection import Connection, ConnectionMessage
    from models.user import User

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return False

    found = await db.scalar(
        select(ConnectionMessage.id)
        .join(Connection, Connection.id == ConnectionMessage.connection_id)
        .where(
            ConnectionMessage.image_url == MEDIA_URL_PREFIX + relpath,
            or_(Connection.sender_id == user_id, Connection.receiver_id == user_id),
        )
        .limit(1)
    )
    return found is not None


@router.get("/{subpath:path}")
async def get_media_file(
    subpath: str,
    db: DbSession,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
    exp: Annotated[Optional[str], Query(max_length=20)] = None,
    sig: Annotated[Optional[str], Query(max_length=128)] = None,
):
    """Serve uploaded media files securely, preventing path traversal attacks."""
    base_dir = Path(settings.UPLOAD_DIR).resolve()
    target_path = (base_dir / subpath).resolve()

    # Prevent path traversal outside the designated upload directory
    try:
        relpath = target_path.relative_to(base_dir).as_posix()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    headers = {"X-Content-Type-Options": "nosniff"}

    top = relpath.split("/", 1)[0]
    if top in PRIVATE_MEDIA_DIRS:
        allowed = verify_media_signature(relpath, exp, sig) or await _bearer_may_view(
            db, credentials, relpath
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This media is private: the link is missing, invalid or expired",
            )
        headers["Cache-Control"] = "private, max-age=3600"
        headers["Referrer-Policy"] = "no-referrer"

    if not target_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    ext = target_path.suffix.lower()
    media_type = INLINE_MEDIA_TYPES.get(ext)
    if media_type is None:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return FileResponse(
            target_path,
            media_type="application/octet-stream",
            filename=target_path.name,
            content_disposition_type="attachment",
            headers=headers,
        )

    return FileResponse(target_path, media_type=media_type, headers=headers)
