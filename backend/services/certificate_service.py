"""Certificate service for managing seller compliance and quality certificates."""

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.certificate import Certificate
from models.enums import CertificationStatus
from models.user import UserProfile
from schemas.certificate import CertificateCreate


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
