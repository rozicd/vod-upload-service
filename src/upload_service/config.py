from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    service_name: str = "upload-service"
    environment: str = "local"

    aws_region: str = "eu-central-1"
    aws_endpoint_url: str | None = "http://localstack:4566"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"

    s3_bucket_name: str = "upload-service-media"
    dynamodb_table_name: str = "upload-service-uploads"

    max_upload_size_bytes: int = 5 * 1024**3  # 5 GiB
    allowed_content_type_prefixes: list[str] = ["video/", "audio/"]

    default_thumbnail_s3_key: str = "_defaults/default-thumbnail.png"

    sns_topic_arn: str = "arn:aws:sns:eu-central-1:000000000000:upload-events"

    # OpenTelemetry
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"
    otel_metrics_port: int = 9464


settings = Settings()
