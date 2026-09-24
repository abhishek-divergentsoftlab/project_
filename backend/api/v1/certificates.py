"""Certificate endpoints for managing seller compliance badges."""

import uuid
from typing import Sequence

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from api.deps import CurrentUser, DbSession
from schemas.certificate import (
    CertificateCreate,
    CertificateDocumentUploadOut,
    CertificateOut,
)
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


@router.post(
    "/upload",
    response_model=CertificateDocumentUploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a certificate document file from local system",
)
async def upload_certificate_file(
    file: UploadFile = File(...),
    current_user: CurrentUser = None,
) -> CertificateDocumentUploadOut:
    """Accept and securely store an uploaded certificate document (PDF, PNG, JPG, WebP)."""
    file_bytes = await file.read()
    try:
        doc_url, filename, size = certificate_service.validate_and_save_certificate_file(
            file_bytes=file_bytes,
            original_filename=file.filename or "certificate.pdf",
            content_type=file.content_type,
        )
    except CertificateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return CertificateDocumentUploadOut(
        document_url=doc_url,
        filename=filename,
        file_size=size,
    )


@router.post(
    "/{certificate_id}/document",
    response_model=CertificateOut,
    summary="Attach or replace a certificate document file on an existing certificate",
)
async def attach_certificate_document(
    certificate_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: CurrentUser = None,
    db: DbSession = None,
) -> CertificateOut:
    """Upload and attach a document to an existing certificate owned by the current user."""
    file_bytes = await file.read()
    try:
        cert = await certificate_service.upload_certificate_document(
            db=db,
            certificate_id=certificate_id,
            user_id=current_user.id,
            file_bytes=file_bytes,
            original_filename=file.filename or "certificate.pdf",
            content_type=file.content_type,
        )
    except CertificateError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return CertificateOut.model_validate(cert)
