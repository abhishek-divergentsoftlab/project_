"""Media streaming and asset serving router."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from core.config import settings

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{subpath:path}")
async def get_media_file(subpath: str):
    """Serve uploaded media files securely, preventing path traversal attacks."""
    base_dir = Path(settings.UPLOAD_DIR).resolve()
    target_path = (base_dir / subpath).resolve()

    # Prevent path traversal outside the designated upload directory
    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if not target_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    # Determine media type based on extension
    ext = target_path.suffix.lower()
    media_type = "application/octet-stream"
    if ext in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"

    return FileResponse(target_path, media_type=media_type)
