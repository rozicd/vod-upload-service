from functools import lru_cache

import boto3
from botocore.config import Config

from upload_service.config import settings

# Fail fast: this is used on the /health/ready path, which must not hang
# for boto3's much longer default timeouts if S3/DynamoDB are unreachable.
_BOTO_CONFIG = Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1})


@lru_cache
def get_s3_client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=_BOTO_CONFIG,
    )


@lru_cache
def get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=_BOTO_CONFIG,
    )


@lru_cache
def get_sns_client():
    # Publish-only, not a long-polling consumer — the fail-fast short
    # timeout above is fine here, unlike a receive_message long-poll which
    # would need a longer read_timeout.
    return boto3.client(
        "sns",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=_BOTO_CONFIG,
    )
