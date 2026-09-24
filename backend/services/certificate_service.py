"""Certificate service for managing seller compliance and quality certificates."""

import uuid
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.certificate import Certificate
from models.enums import CertificationStatus
from models.user import UserProfile
from schemas.certificate import CertificateCreate

MAX_CERTIFICATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CERTIFICATE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


class CertificateError(Exception):
    """Domain-level certificate error."""


async def create_certificate(
    db: AsyncSession, user_id: uuid.UUID, payload: CertificateCreate
) -> Certificate:
    if payload.expiry_date and payload.expiry_date <= payload.issue_date:
        raise CertificateError("Certificate expiry date must be after the issue date")

    certificate = Certificate(
        user_id=user_id,
        name=payload.name.strip(),
        issuing_body=payload.issuing_body.strip(),
        certificate_number=payload.certificate_number.strip(),
        issue_date=payload.issue_date,
        expiry_date=payload.expiry_date,
        document_url=payload.document_url,
        verification_status=CertificationStatus.VERIFIED,
    )
    db.add(certificate)

    # Boost trust score by 10 for having a verified industry certificate
    profile_query = select(UserProfile).where(UserProfile.user_id == user_id)
    profile = (await db.execute(profile_query)).scalar_one_or_none()
    if profile:
        profile.trust_score = min(100, profile.trust_score + 10)

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

    random_filename = f"cert_{uuid.uuid4().hex}{ext}"
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
    )

    cert.document_url = document_url
    await db.commit()
    await db.refresh(cert)
    return cert
