import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from opentelemetry import metrics

from upload_service.clients.aws import get_dynamodb_resource, get_s3_client, get_sns_client
from upload_service.config import settings
from upload_service.schemas import MediaAsset, ThumbnailAsset, UploadResponse

logger = logging.getLogger(__name__)

_DEFAULT_THUMBNAIL_ASSET = Path(__file__).resolve().parent.parent / "assets" / "default_thumbnail.png"
_DEFAULT_THUMBNAIL_CONTENT_TYPE = "image/png"
_THUMBNAIL_CONTENT_TYPE_PREFIXES = ["image/"]
_UPLOAD_CREATED_EVENT_TYPE = "upload.created"

_meter = metrics.get_meter(__name__)
_uploads_total = _meter.create_counter(
    "uploads_total",
    unit="1",
    description="Number of POST /uploads requests, by outcome",
)
_upload_size_bytes = _meter.create_histogram(
    "upload_size_bytes",
    unit="By",
    description="Size of successfully stored upload content",
)
_upload_events_published_total = _meter.create_counter(
    "upload_events_published_total",
    unit="1",
    description="Number of upload.created events published to the upload-events SNS topic, by outcome",
)


class UploadValidationError(Exception):
    """The incoming upload failed validation (bad content-type, too large)."""


class UploadStorageError(Exception):
    """Writing to S3/DynamoDB failed after validation passed."""


def create_upload(
    *,
    title: str,
    description: str | None,
    tags: list[str],
    content_file: UploadFile,
    thumbnail_file: UploadFile | None,
) -> UploadResponse:
    try:
        record = _create_upload(
            title=title,
            description=description,
            tags=tags,
            content_file=content_file,
            thumbnail_file=thumbnail_file,
        )
    except UploadValidationError:
        _uploads_total.add(1, {"status": "validation_error"})
        raise
    except UploadStorageError:
        _uploads_total.add(1, {"status": "storage_error"})
        raise
    _uploads_total.add(1, {"status": "stored"})
    _upload_size_bytes.record(record.content.size_bytes)
    return record


def _create_upload(
    *,
    title: str,
    description: str | None,
    tags: list[str],
    content_file: UploadFile,
    thumbnail_file: UploadFile | None,
) -> UploadResponse:
    # title/description/tags are catalog-service's data, not upload-service's
    # — accepted here so they can be forwarded in the upload.created event
    # below, but never persisted to or returned from upload-service's own
    # ingestion record.
    _validate_content_type(content_file.content_type, settings.allowed_content_type_prefixes, "content")
    if thumbnail_file is not None:
        _validate_content_type(thumbnail_file.content_type, _THUMBNAIL_CONTENT_TYPE_PREFIXES, "thumbnail")

    upload_id = str(uuid4())
    s3_client = get_s3_client()

    content_asset = _upload_asset(s3_client, content_file, key=f"{upload_id}/content/{content_file.filename}")

    try:
        thumbnail_asset = _resolve_thumbnail(s3_client, upload_id, thumbnail_file)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("failed to store thumbnail for upload %s", upload_id)
        _delete_object_best_effort(s3_client, content_asset.s3_bucket, content_asset.s3_key)
        raise UploadStorageError("failed to store thumbnail") from exc

    record = UploadResponse(
        upload_id=upload_id,
        content=content_asset,
        thumbnail=thumbnail_asset,
        status="stored",
        created_at=datetime.now(timezone.utc),
    )

    try:
        table = get_dynamodb_resource().Table(settings.dynamodb_table_name)
        table.put_item(Item=_to_item(record))
    except (BotoCoreError, ClientError) as exc:
        logger.exception("failed to write metadata for upload %s", upload_id)
        _delete_object_best_effort(s3_client, content_asset.s3_bucket, content_asset.s3_key)
        if not thumbnail_asset.is_default:
            _delete_object_best_effort(s3_client, thumbnail_asset.s3_bucket, thumbnail_asset.s3_key)
        raise UploadStorageError("failed to write upload metadata") from exc

    _publish_upload_created_event(record, title=title, description=description, tags=tags)
    return record


def _publish_upload_created_event(
    record: UploadResponse, *, title: str, description: str | None, tags: list[str]
) -> None:
    """Fail-open by design: catalog-service's consumer was built assuming a
    dropped publish here is a realistic "silent dependency failure" chaos
    scenario, not a hard coupling — the upload already succeeded and must
    not be rolled back for this. See HANDOFF.md / CLAUDE.md → "Integration
    with catalog-service".

    Published to the `upload-events` SNS topic, not directly to a queue —
    catalog-service and transcoding-service each subscribe their own SQS
    queue to this topic (a single SQS queue can't deliver one message to two
    independent consumers). Both subscriptions use RawMessageDelivery=true
    (see vod-infra/localstack-init/init-aws.sh) so the queue's message Body is
    this exact JSON payload, unwrapped — consumers don't need to know SNS is
    involved at all."""
    payload = {
        "event_type": _UPLOAD_CREATED_EVENT_TYPE,
        "upload_id": record.upload_id,
        "title": title,
        "description": description,
        "tags": tags,
        "content": {
            "s3_bucket": record.content.s3_bucket,
            "s3_key": record.content.s3_key,
            "content_type": record.content.content_type,
        },
        "thumbnail": {
            "s3_bucket": record.thumbnail.s3_bucket,
            "s3_key": record.thumbnail.s3_key,
            "content_type": record.thumbnail.content_type,
        },
        "created_at": record.created_at.isoformat(),
    }
    try:
        get_sns_client().publish(TopicArn=settings.sns_topic_arn, Message=json.dumps(payload))
    except (BotoCoreError, ClientError):
        logger.exception("failed to publish upload.created event for upload %s", record.upload_id)
        _upload_events_published_total.add(1, {"status": "failed"})
        return
    _upload_events_published_total.add(1, {"status": "ok"})


def get_upload(upload_id: str) -> UploadResponse | None:
    table = get_dynamodb_resource().Table(settings.dynamodb_table_name)
    try:
        item = table.get_item(Key={"upload_id": upload_id}).get("Item")
    except (BotoCoreError, ClientError) as exc:
        logger.exception("failed to read upload %s", upload_id)
        raise UploadStorageError("failed to read upload metadata") from exc
    return _from_item(item) if item is not None else None


def _resolve_thumbnail(s3_client, upload_id: str, thumbnail_file: UploadFile | None) -> ThumbnailAsset:
    if thumbnail_file is not None:
        uploaded = _upload_asset(s3_client, thumbnail_file, key=f"{upload_id}/thumbnail/{thumbnail_file.filename}")
        return ThumbnailAsset(
            s3_bucket=uploaded.s3_bucket,
            s3_key=uploaded.s3_key,
            content_type=uploaded.content_type,
            is_default=False,
        )
    _ensure_default_thumbnail(s3_client)
    return ThumbnailAsset(
        s3_bucket=settings.s3_bucket_name,
        s3_key=settings.default_thumbnail_s3_key,
        content_type=_DEFAULT_THUMBNAIL_CONTENT_TYPE,
        is_default=True,
    )


def _ensure_default_thumbnail(s3_client) -> None:
    """Lazily seed the shared default thumbnail into S3 so every upload's
    `thumbnail` field is a real, uniform S3 reference — no manual
    provisioning step needed in any environment."""
    try:
        s3_client.head_object(Bucket=settings.s3_bucket_name, Key=settings.default_thumbnail_s3_key)
        return
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3_client.upload_file(
        str(_DEFAULT_THUMBNAIL_ASSET),
        settings.s3_bucket_name,
        settings.default_thumbnail_s3_key,
        ExtraArgs={"ContentType": _DEFAULT_THUMBNAIL_CONTENT_TYPE},
    )


def _upload_asset(s3_client, file: UploadFile, *, key: str) -> MediaAsset:
    size_bytes = _file_size(file)
    if size_bytes > settings.max_upload_size_bytes:
        raise UploadValidationError(f"file exceeds max upload size of {settings.max_upload_size_bytes} bytes")

    content_type = file.content_type or "application/octet-stream"
    try:
        s3_client.upload_fileobj(
            file.file,
            settings.s3_bucket_name,
            key,
            ExtraArgs={"ContentType": content_type},
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("failed to upload %s to S3", key)
        raise UploadStorageError(f"failed to store {key}") from exc

    return MediaAsset(
        s3_bucket=settings.s3_bucket_name,
        s3_key=key,
        content_type=content_type,
        size_bytes=size_bytes,
        original_filename=file.filename or "unnamed",
    )


def _file_size(file: UploadFile) -> int:
    # Starlette has already fully spooled the multipart part to a
    # SpooledTemporaryFile by the time our code runs, so this is a cheap
    # seek/tell rather than a second full read.
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    return size


def _validate_content_type(content_type: str | None, allowed_prefixes: list[str], label: str) -> None:
    if not content_type or not any(content_type.startswith(prefix) for prefix in allowed_prefixes):
        raise UploadValidationError(
            f"{label} content-type {content_type!r} is not allowed (expected one of {allowed_prefixes})"
        )


def _delete_object_best_effort(s3_client, bucket: str, key: str) -> None:
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError):
        logger.exception("failed to clean up orphaned object s3://%s/%s", bucket, key)


def _to_item(record: UploadResponse) -> dict:
    return {
        "upload_id": record.upload_id,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "content_s3_bucket": record.content.s3_bucket,
        "content_s3_key": record.content.s3_key,
        "content_content_type": record.content.content_type,
        "content_size_bytes": record.content.size_bytes,
        "content_original_filename": record.content.original_filename,
        "thumbnail_s3_bucket": record.thumbnail.s3_bucket,
        "thumbnail_s3_key": record.thumbnail.s3_key,
        "thumbnail_content_type": record.thumbnail.content_type,
        "thumbnail_is_default": record.thumbnail.is_default,
    }


def _from_item(item: dict) -> UploadResponse:
    return UploadResponse(
        upload_id=item["upload_id"],
        status=item["status"],
        created_at=item["created_at"],
        content=MediaAsset(
            s3_bucket=item["content_s3_bucket"],
            s3_key=item["content_s3_key"],
            content_type=item["content_content_type"],
            size_bytes=int(item["content_size_bytes"]),
            original_filename=item["content_original_filename"],
        ),
        thumbnail=ThumbnailAsset(
            s3_bucket=item["thumbnail_s3_bucket"],
            s3_key=item["thumbnail_s3_key"],
            content_type=item["thumbnail_content_type"],
            is_default=bool(item["thumbnail_is_default"]),
        ),
    )
