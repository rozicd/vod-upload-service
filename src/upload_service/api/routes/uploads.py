from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from upload_service.schemas import UploadResponse
from upload_service.services import uploads as uploads_service

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def create_upload(
    title: Annotated[str, Form()],
    content_file: Annotated[UploadFile, File()],
    description: Annotated[str | None, Form()] = None,
    tags: Annotated[list[str] | None, Form()] = None,
    thumbnail_file: Annotated[UploadFile | None, File()] = None,
) -> UploadResponse:
    try:
        return uploads_service.create_upload(
            title=title,
            description=description,
            tags=tags or [],
            content_file=content_file,
            thumbnail_file=thumbnail_file,
        )
    except uploads_service.UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except uploads_service.UploadStorageError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/{upload_id}", response_model=UploadResponse)
def get_upload(upload_id: str) -> UploadResponse:
    try:
        record = uploads_service.get_upload(upload_id)
    except uploads_service.UploadStorageError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="upload not found")
    return record
