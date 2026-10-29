"""Operator review of KYC claims and certificates.

Every route here depends on OperatorUser (see api/deps.py), so only accounts
listed in settings.OPERATOR_EMAILS can reach it.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ops/trust", tags=["ops"])

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.deps import DbSession, OperatorUser
from models.enums import CertificationStatus, KYCStatus
from schemas.certificate import CertificateOut
from services import certificate_service, kyc_service
from services.certificate_service import CertificateError
from services.kyc_service import KYCError


class OtherClaimant(BaseModel):
    user_id: uuid.UUID
    email: str
    kyc_status: KYCStatus


class KYCClaimOut(BaseModel):
    user_id: uuid.UUID
    email: str
    company_name: Optional[str] = None
    gst_number: Optional[str] = None
    legal_business_name: Optional[str] = None
    kyc_status: KYCStatus
    submitted_at: datetime
    other_claimants: list[OtherClaimant] = []


class KYCDecisionOut(BaseModel):
    user_id: uuid.UUID
    kyc_status: KYCStatus
    trust_score: int


class RejectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class OpsCertificateOut(CertificateOut):
    email: str
    company_name: Optional[str] = None


@router.get("/kyc", response_model=list[KYCClaimOut], summary="KYC claims awaiting review")
async def list_kyc_claims(
    _: OperatorUser,
    db: DbSession,
    status: Literal["pending", "flagged"] = Query("pending"),
) -> list[KYCClaimOut]:
    items = await kyc_service.list_kyc_claims(db, KYCStatus(status))
    return [KYCClaimOut(**item) for item in items]


@router.post("/kyc/{user_id}/approve", response_model=KYCDecisionOut, summary="Approve a KYC claim")
async def approve_kyc(user_id: uuid.UUID, _: OperatorUser, db: DbSession) -> KYCDecisionOut:
    try:
        profile = await kyc_service.approve_kyc(db, user_id)
    except KYCError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return KYCDecisionOut(user_id=user_id, kyc_status=profile.kyc_status, trust_score=profile.trust_score)


@router.post("/kyc/{user_id}/reject", response_model=KYCDecisionOut, summary="Reject a KYC claim")
async def reject_kyc(
    user_id: uuid.UUID, payload: RejectPayload, _: OperatorUser, db: DbSession
) -> KYCDecisionOut:
    try:
        profile = await kyc_service.reject_kyc(db, user_id, payload.reason.strip())
    except KYCError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return KYCDecisionOut(user_id=user_id, kyc_status=profile.kyc_status, trust_score=profile.trust_score)


@router.get(
    "/certificates", response_model=list[OpsCertificateOut], summary="Certificates awaiting review"
)
async def list_certificates(
    _: OperatorUser,
    db: DbSession,
    status: Literal["pending"] = Query("pending"),
) -> list[OpsCertificateOut]:
    items = await certificate_service.list_certificates_for_review(db, CertificationStatus(status))
    return [OpsCertificateOut(**item) for item in items]


@router.post(
    "/certificates/{certificate_id}/approve",
    response_model=CertificateOut,
    summary="Approve a certificate",
)
async def approve_certificate(
    certificate_id: uuid.UUID, _: OperatorUser, db: DbSession
) -> CertificateOut:
    try:
        cert = await certificate_service.review_certificate(db, certificate_id, approve=True)
    except CertificateError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return CertificateOut.model_validate(cert)


@router.post(
    "/certificates/{certificate_id}/reject",
    response_model=CertificateOut,
    summary="Reject a certificate",
)
async def reject_certificate(
    certificate_id: uuid.UUID, payload: RejectPayload, _: OperatorUser, db: DbSession
) -> CertificateOut:
    try:
        cert = await certificate_service.review_certificate(
            db, certificate_id, approve=False, reason=payload.reason.strip()
        )
    except CertificateError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return CertificateOut.model_validate(cert)
