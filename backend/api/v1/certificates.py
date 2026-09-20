"""Certificate endpoints for managing seller compliance badges."""

import uuid
from typing import Sequence

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from schemas.certificate import CertificateCreate, CertificateOut
from services import certificate_service
from services.certificate_service import CertificateError

router = APIRouter()


@router.get(
    "",
    response_model=list[CertificateOut],
    summary="List all certificates of currently authenticated user",
)
async def list_own_certificates(
    current_user: CurrentUser,
    db: DbSession,
) -> Sequence[CertificateOut]:
    certs = await certificate_service.list_user_certificates(db, current_user.id)
    return [CertificateOut.model_validate(c) for c in certs]


@router.post(
    "",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new certificate",
)
async def create_certificate(
    payload: CertificateCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> CertificateOut:
    try:
        cert = await certificate_service.create_certificate(
            db, user_id=current_user.id, payload=payload
        )
    except CertificateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return CertificateOut.model_validate(cert)


@router.delete(
    "/{certificate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a certificate",
)
async def delete_certificate(
    certificate_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> None:
    try:
        await certificate_service.delete_certificate(
            db, certificate_id=certificate_id, user_id=current_user.id
        )
    except CertificateError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get(
    "/user/{user_id}",
    response_model=list[CertificateOut],
    summary="Get verified certificates for any public enterprise",
)
async def get_public_certificates(
    user_id: uuid.UUID,
    db: DbSession,
) -> Sequence[CertificateOut]:
    certs = await certificate_service.get_public_certificates(db, user_id)
    return [CertificateOut.model_validate(c) for c in certs]
