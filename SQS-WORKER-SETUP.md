# SQS worker pattern — setup guide

How `catalog-service` consumes SQS events, written up as a reusable pattern
for wiring the same thing into another service (e.g. `transcoding-service`
consuming its own queue, or `upload-service` publishing onto
`upload-events`). `catalog-service` is the reference implementation — file
paths below point at it.

## Shape of the pattern

**Consumer runs as its own process/container, not an in-process thread.**
Same Docker image as the service's API, just a different `command`. This
matters for this project specifically: the chaos-injection protocol wants
"consumer down" to be a distinct, independently-injectable fault from "API
down" (`docker compose stop <service>-worker` should degrade ingestion while
the API keeps serving stale-but-available data), which an in-process thread
can't produce.

**Failed messages retry via SQS's visibility timeout, then land on a DLQ.**
The worker only deletes a message after it's fully processed; on failure it
leaves the message alone so SQS redelivers it, and a redrive policy sends it
to a dead-letter queue after N failed attempts so one poison message can't
stall the whole queue forever.

---

## 1. LocalStack: enable SQS

`vod-infra/docker-compose.yml`, `localstack` service:

```yaml
environment:
  - SERVICES=s3,dynamodb,sqs   # add sqs
```

## 2. LocalStack init script: create the queue + DLQ with a redrive policy

`vod-infra/localstack-init/init-aws.sh` — create the DLQ first, look up its ARN,
then create the main queue with `RedrivePolicy` pointing at it:

```bash
DLQ_URL=$(awslocal sqs create-queue --queue-name <queue-name>-dlq --query 'QueueUrl' --output text)
DLQ_ARN=$(awslocal sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

# Built with python3 (present in the localstack image) rather than hand-escaped
# shell string concatenation — RedrivePolicy's value is itself a JSON string
# nested inside the --attributes JSON, which is a nuisance to get right by hand.
QUEUE_ATTRS=$(python3 -c "
import json, sys
redrive_policy = json.dumps({'deadLetterTargetArn': sys.argv[1], 'maxReceiveCount': '5'})
print(json.dumps({'RedrivePolicy': redrive_policy}))
" "$DLQ_ARN")

awslocal sqs create-queue --queue-name <queue-name> --attributes "$QUEUE_ATTRS"
```

Don't hand-build the nested JSON string with backslash-escaped quotes in
bash — it's easy to get subtly wrong and hard to review. Shelling out to
`python3 -c` (already present in the `localstack/localstack` image) to build
it with `json.dumps` twice (once for the inner `RedrivePolicy` value, once
for the outer `--attributes` object) is worth the extra line.

`maxReceiveCount: 5` is a reasonable default; tune per queue if a scenario
needs faster/slower DLQ landing.

## 3. Config: queue URL + wait time

`config.py`:

```python
sqs_queue_url: str = "http://localstack:4566/000000000000/<queue-name>"
sqs_wait_time_seconds: int = 20
```

LocalStack's account ID is always `000000000000`, so the URL is predictable
without a runtime lookup — same as how table/bucket names are already
hardcoded as config defaults elsewhere in this codebase.

Mirror both into `.env.example`.

## 4. AWS client: separate timeout config from other clients

`clients/aws.py` — an SQS client needs a **longer read timeout** than a
fail-fast client (e.g. one used on a `/health/ready` path), because
`receive_message`'s long-poll genuinely blocks for up to `WaitTimeSeconds`:

```python
_SQS_BOTO_CONFIG = Config(
    connect_timeout=5,
    read_timeout=settings.sqs_wait_time_seconds + 5,
    retries={"max_attempts": 1},
)

@lru_cache
def get_sqs_client():
    return boto3.client(
        "sqs",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=_SQS_BOTO_CONFIG,
    )
```

A client reused from a short-timeout config (e.g. 2s, sized for a health
check) will abort the long-poll itself, not just a slow request.

## 5. Service layer: one idempotent ingest function

Keep it a plain function in the existing service module, not a new layer —
same layering convention as everything else (routes/services/schemas).

```python
def ingest_<event>(event: dict) -> None:
    """Idempotent by construction: a put_item keyed by the event's own ID,
    so redelivery just overwrites the same item with the same data."""
    record = _from_event(event)
    try:
        table.put_item(Item=_to_item(record))
    except (BotoCoreError, ClientError) as exc:
        logger.exception(...)
        raise <ServiceName>StorageError(...) from exc
```

Extract only the fields you actually own from the event payload — don't
`**kwargs`-unpack the producer's shape wholesale. The producer's own record
almost certainly carries extra fields your service doesn't need (e.g.
upload-service's asset structs carry `size_bytes`/`original_filename` that
catalog-service's `MediaAssetRef` doesn't have); pulling out named fields
explicitly documents exactly what the contract is, rather than coupling
silently to whatever the producer happens to send.

Add an outcome-labeled counter next to it, matching whatever metric
convention the service already has for its other write paths (e.g.
`<thing>_ingested_total{status}`).

## 6. The worker itself

Structure it as **one testable poll function + a thin infinite-loop
wrapper**, not one big `while True` — that's what makes it unit-testable:

```python
def poll_once(sqs_client) -> int:
    response = sqs_client.receive_message(
        QueueUrl=settings.sqs_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=settings.sqs_wait_time_seconds,
    )
    messages = response.get("Messages", [])
    for message in messages:
        _process_message(sqs_client, message)
    return len(messages)

def _process_message(sqs_client, message) -> None:
    try:
        ingest_<event>(json.loads(message["Body"]))
    except (json.JSONDecodeError, KeyError, <ServiceName>StorageError):
        logger.exception("failed to process message %s; leaving for redelivery", message.get("MessageId"))
        return  # don't delete — SQS redelivers, redrive policy handles poison messages
    sqs_client.delete_message(QueueUrl=settings.sqs_queue_url, ReceiptHandle=message["ReceiptHandle"])

def main() -> None:
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    setup_telemetry()
    sqs_client = get_sqs_client()
    while not _shutdown:
        poll_once(sqs_client)
```

Handle `SIGTERM`/`SIGINT` by setting a flag checked between polls, so
`docker compose stop`/a chaos scenario killing the container finishes the
current poll instead of dying mid-write.

## 7. Telemetry: make `setup_telemetry()` app-optional

If `telemetry.py`'s setup function currently requires a `FastAPI` app (for
`FastAPIInstrumentor`), make that parameter optional so the worker — which
has no FastAPI app — can still get traces/metrics/botocore instrumentation:

```python
def setup_telemetry(app: FastAPI | None = None) -> None:
    ...
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)
    BotocoreInstrumentor().instrument()
```

Set a distinct `SERVICE_NAME` env var per process (e.g. `<service>` vs.
`<service>-worker`) so traces/metrics are attributable to the right process
in Jaeger/Prometheus — this matters more here than in most projects, since
the whole point is observability-driven RCA.

## 8. docker-compose: second service, same image, different command

```yaml
<service>-worker:
  build:
    context: ../<service>
    dockerfile: Dockerfile
  command: ["python", "-m", "<package>.worker"]
  environment:
    SERVICE_NAME: <service>-worker
    # ...same AWS/queue env as the API service...
  depends_on:
    localstack:
      condition: service_healthy
```

No `ports:` needed unless you're exposing its metrics port to something
that scrapes it — it has no HTTP server.

## 9. Tests: moto covers SQS out of the box, but watch two gotchas

- `moto`'s unified `mock_aws()` already mocks SQS along with whatever else
  you're mocking (DynamoDB, S3, ...) in the same `with` block — no separate
  `moto[sqs]` extra needed beyond whatever's already installed.
- **`moto`'s mocked `receive_message` genuinely sleeps for `WaitTimeSeconds`**
  — it is not simulated/instant. A test fixture that creates the queue
  should force `settings.sqs_wait_time_seconds = 0` for the duration of the
  test, or every `poll_once()` call in your test suite takes ~20 real
  seconds. (Confirmed by timing it directly — 20.0s elapsed for a
  `WaitTimeSeconds=20` call against an empty mocked queue.)
- Structure tests around `poll_once(sqs_client)` directly (send a message
  with `sqs_client.send_message`, call `poll_once`, assert on the resulting
  DB state + that the message was deleted/left in place) — don't test
  through `main()`'s infinite loop.

---

## If the other service is a *producer* instead (e.g. upload-service)

Not built yet anywhere in this codebase, but the shape is simpler — no
worker process needed, just a `send_message` call from the existing
request-handling code path:

```python
def _publish_event(payload: dict) -> None:
    try:
        get_sqs_client().send_message(QueueUrl=settings.sqs_queue_url, MessageBody=json.dumps(payload))
    except (BotoCoreError, ClientError):
        logger.exception("failed to publish event")
        # decide fail-open vs fail-closed here — does a publish failure fail
        # the whole request, or just get logged? (catalog-service's worker
        # was designed assuming upload-service leans fail-open, so a
        # dropped/failed publish is a realistic "silent dependency failure"
        # scenario rather than a hard coupling — confirm this is still the
        # intended chaos scenario before implementing.)
```

Same `get_sqs_client()`/timeout-config approach as above applies (though a
publish-only client doesn't need the long read timeout a long-polling
consumer needs — a short one is fine, `send_message` doesn't block on
`WaitTimeSeconds`).
