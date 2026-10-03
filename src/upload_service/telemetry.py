from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import start_http_server

from upload_service.config import settings


def setup_telemetry(app: FastAPI) -> None:
    """Wire up traces (OTLP -> Jaeger) and metrics (OTel SDK -> Prometheus scrape endpoint).

    Traces are pushed via OTLP/gRPC; metrics are pulled by Prometheus from a
    dedicated port exposed by this process (no collector in front, on purpose,
    to keep the skeleton simple).
    """
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment": settings.environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        )
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    start_http_server(port=settings.otel_metrics_port)

    FastAPIInstrumentor.instrument_app(app)
    BotocoreInstrumentor().instrument()
