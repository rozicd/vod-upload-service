from datetime import datetime

from pydantic import BaseModel


class MediaAsset(BaseModel):
    s3_bucket: str
    s3_key: str
    content_type: str
    size_bytes: int
    original_filename: str


class ThumbnailAsset(BaseModel):
    s3_bucket: str
    s3_key: str
    content_type: str
    is_default: bool


class UploadResponse(BaseModel):
    """upload-service's own ingestion record: what was uploaded, where the
    blobs live, upload status. Browsable metadata (title/description/tags)
    is catalog-service's concern, not persisted or returned here — see
    "Integration with catalog-service" in this service's CLAUDE.md."""

    upload_id: str
    content: MediaAsset
    thumbnail: ThumbnailAsset
    status: str
    created_at: datetime
