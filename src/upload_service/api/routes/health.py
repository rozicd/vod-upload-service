from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from upload_service.clients.aws import get_dynamodb_resource, get_s3_client
from upload_service.config import settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def liveness() -> dict:
    """Process is up. No dependency checks — used for restart decisions."""
    return {"status": "ok"}


@router.get("/ready")
def readiness() -> JSONResponse:
    """Process can serve traffic — checks that S3 and DynamoDB are reachable."""
    checks = {"s3": _check_s3(), "dynamodb": _check_dynamodb()}
    healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


def _check_s3() -> bool:
    try:
        get_s3_client().head_bucket(Bucket=settings.s3_bucket_name)
        return True
    except (BotoCoreError, ClientError):
        return False


def _check_dynamodb() -> bool:
    try:
        table = get_dynamodb_resource().Table(settings.dynamodb_table_name)
        table.load()
        return True
    except (BotoCoreError, ClientError):
        return False
