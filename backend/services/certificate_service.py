"""Certificate service for managing seller compliance and quality certificates.

Certificates are self-declared: nobody reviews them yet. ``verification_status``
keeps the existing enum value, but the trust-score effect is bounded and
derived from facts: ``kyc_service.recompute_trust_score`` counts the user's
currently valid (unexpired) certificates at +5 each, capped at +15, so deleting
or expiring one removes its bonus and certificates cannot farm the score.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.certificate import Certificate
from models.enums import CertificationStatus
from models.user import UserProfile
from schemas.certificate import CertificateCreate
from services import kyc_service

MAX_CERTIFICATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CERTIFICATE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
CERTIFICATE_MEDIA_PREFIX = "/api/v1/media/certificates/"

# Uploads are named cert_<owner uuid hex>_<random hex>.<ext> so a document URL
# can be tied back to the account that uploaded it. Files from before this
# scheme (cert_<random hex>.<ext>) are still served, but cannot be attached to
# a new certificate because their owner is unknown.
_OWNED_UPLOAD_RE = re.compile(
    r"^cert_(?P<owner>[0-9a-f]{32})_[0-9a-f]{32}\.(?:pdf|png|jpg|jpeg|webp)$"
)


class CertificateError(Exception):
    """Domain-level certificate error. ``status_code`` maps it onto HTTP."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def validate_document_url(document_url: Optional[str], user_id: uuid.UUID) -> Optional[str]:
    """Allow only empty or a certificate file this user uploaded through us.

    Rejects ``javascript:`` / ``data:`` URLs, external links, other users'
    uploads, path tricks, and files that do not exist.
    """
    if document_url is None:
        return None
    url = document_url.strip()
    if not url:
        return None

    if not url.startswith(CERTIFICATE_MEDIA_PREFIX):
        raise CertificateError(
            "document_url must be a file uploaded via /certifications/upload", status_code=422
        )
    filename = url[len(CERTIFICATE_MEDIA_PREFIX):]
    match = _OWNED_UPLOAD_RE.match(filename)
    if match is None:
        raise CertificateError(
            "document_url must be a file uploaded via /certifications/upload", status_code=422
        )
    if match.group("owner") != user_id.hex:
        raise CertificateError(
            "document_url refers to a file uploaded by another account", status_code=403
        )
    if not (Path(settings.UPLOAD_DIR).resolve() / "certificates" / filename).is_file():
        raise CertificateError("document_url refers to a file that does not exist", status_code=422)
    return url


async def _refresh_trust(db: AsyncSession, user_id: uuid.UUID) -> None:
    profile = (
        await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is not None:
        await kyc_service.recompute_trust_score(db, profile)


async def create_certificate(
    db: AsyncSession, user_id: uuid.UUID, payload: CertificateCreate
) -> Certificate:
    now = datetime.now(UTC)
    issue_date = _aware(payload.issue_date)
    expiry_date = _aware(payload.expiry_date) if payload.expiry_date else None

    if issue_date > now + timedelta(days=1):
        raise CertificateError("Certificate issue date cannot be in the future", status_code=422)
    if expiry_date and expiry_date <= issue_date:
        raise CertificateError("Certificate expiry date must be after the issue date", status_code=422)
    if expiry_date and expiry_date <= now:
        raise CertificateError(
            "Certificate has already expired; only currently valid certificates can be added",
            status_code=422,
        )

    document_url = validate_document_url(payload.document_url, user_id)

    certificate = Certificate(
        user_id=user_id,
        name=payload.name.strip(),
        issuing_body=payload.issuing_body.strip(),
        certificate_number=payload.certificate_number.strip(),
        issue_date=payload.issue_date,
        expiry_date=payload.expiry_date,
        document_url=document_url,
        verification_status=CertificationStatus.VERIFIED,
    )
    db.add(certificate)
    await db.flush()

    # Recomputed from the user's valid certificates (capped), never incremented.
    await _refresh_trust(db, user_id)

    await db.commit()
    await db.refresh(certificate)
    return certificate


async def list_user_certificates(
    db: AsyncSession, user_id: uuid.UUID
) -> Sequence[Certificate]:
    query = (
        select(Certificate)
        .where(Certificate.user_id == user_id)
        .order_by(Certificate.created_at.desc())
    )
    result = await db.execute(query)
    return result.scalars().all()


async def get_public_certificates(
    db: AsyncSession, user_id: uuid.UUID
) -> Sequence[Certificate]:
    query = (
        select(Certificate)
        .where(
            Certificate.user_id == user_id,
            Certificate.verification_status == CertificationStatus.VERIFIED,
        )
        .order_by(Certificate.created_at.desc())
    )
    result = await db.execute(query)
    return result.scalars().all()


async def delete_certificate(
    db: AsyncSession, certificate_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    query = select(Certificate).where(
        Certificate.id == certificate_id,
        Certificate.user_id == user_id,
    )
    result = await db.execute(query)
    cert = result.scalar_one_or_none()
    if cert is None:
        raise CertificateError("Certificate not found or not owned by you")

    await db.delete(cert)
    await db.flush()
    await _refresh_trust(db, user_id)
    await db.commit()
    return True


def _verify_magic_bytes(file_bytes: bytes, ext: str) -> bool:
    """Validate binary header/magic bytes to prevent MIME-sniffing or extension-spoofing attacks."""
    if ext == ".pdf":
        return file_bytes.startswith(b"%PDF-")
    if ext == ".png":
        return file_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return file_bytes.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return file_bytes.startswith(b"RIFF") and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP"
    return False


def validate_and_save_certificate_file(
    file_bytes: bytes,
    original_filename: str,
    content_type: Optional[str] = None,
    *,
    owner_id: uuid.UUID,
) -> tuple[str, str, int]:
    """Validate certificate file integrity, extension, size, and store it outside web root.

    TODO(security): In production deployment, connect an antivirus scanner or CDR pipeline
    to strip active content before permanent cloud storage.
    """
    if not file_bytes:
        raise CertificateError("Uploaded certificate file is empty")

    file_size = len(file_bytes)
    if file_size > MAX_CERTIFICATE_FILE_SIZE:
        raise CertificateError(
            f"File size ({file_size / (1024 * 1024):.1f} MB) exceeds maximum allowed size of 10 MB"
        )

    # Sanitize original filename (prevent path traversal like ../)
    safe_original_name = Path(original_filename or "certificate.pdf").name.strip()
    ext = Path(safe_original_name).suffix.lower()

    if not ext or ext not in ALLOWED_CERTIFICATE_EXTENSIONS:
        raise CertificateError(
            f"Unsupported file extension '{ext}'. Allowed extensions: {', '.join(sorted(ALLOWED_CERTIFICATE_EXTENSIONS))}"
        )

    # Inspect magic bytes header
    if not _verify_magic_bytes(file_bytes, ext):
        raise CertificateError(
            f"File content does not match expected format for {ext}. Upload rejected for security reasons."
        )

    # Save to dedicated uploads/certificates directory with random UUID
    upload_dir = Path(settings.UPLOAD_DIR).resolve() / "certificates"
    upload_dir.mkdir(parents=True, exist_ok=True)

    random_filename = f"cert_{owner_id.hex}_{uuid.uuid4().hex}{ext}"
    dest_path = (upload_dir / random_filename).resolve()

    # Verify path confinement inside upload_dir
    try:
        dest_path.relative_to(upload_dir)
    except ValueError:
        raise CertificateError("Invalid destination path")

    dest_path.write_bytes(file_bytes)

    document_url = f"/api/v1/media/certificates/{random_filename}"
    return document_url, safe_original_name, file_size


async def upload_certificate_document(
    db: AsyncSession,
    certificate_id: uuid.UUID,
    user_id: uuid.UUID,
    file_bytes: bytes,
    original_filename: str,
    content_type: Optional[str] = None,
) -> Certificate:
    """Attach an uploaded document to an existing certificate owned by user."""
    query = select(Certificate).where(
        Certificate.id == certificate_id,
        Certificate.user_id == user_id,
    )
    result = await db.execute(query)
    cert = result.scalar_one_or_none()
    if cert is None:
        raise CertificateError("Certificate not found or not owned by you")

    document_url, _, _ = validate_and_save_certificate_file(
        file_bytes=file_bytes,
        original_filename=original_filename,
        content_type=content_type,
        owner_id=user_id,
    )

    cert.document_url = document_url
    await db.commit()
    await db.refresh(cert)
    return cert
