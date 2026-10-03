# upload-service — handoff summary (updated 2026-08-28)

Point-in-time summary of what's built in `upload-service`, for briefing
other agents/sessions working on `vod-infra/` (docker-compose) or
`catalog-service`. See `vod-upload-service/CLAUDE.md` for full conventions —
this file is the "what exists and what depends on it" cut.

## What's built

- `POST /uploads` and `GET /uploads/{upload_id}` — see
  `src/upload_service/api/routes/uploads.py`,
  `src/upload_service/services/uploads.py`,
  `src/upload_service/schemas.py`.
- Health checks (`GET /health/live`, `GET /health/ready`), OTel traces
  (auto-instrumented FastAPI + boto3, exported to Jaeger) and three custom
  metrics (`uploads_total{status}`, `upload_size_bytes`,
  `upload_events_published_total{status}`), scraped by Prometheus on port
  `9464`.
- **SQS publish to catalog-service is now built** (was deferred, now
  shipped — see "SQS → catalog-service" below, no longer "planned").
- **No formal test suite yet** (deliberately dropped for now). `pytest`,
  `httpx`, `moto[s3,dynamodb]` are installed as dev dependencies; verified
  so far via ad-hoc scratch scripts, not committed tests.

## API contract (what exists today)

`POST /uploads` — multipart/form-data:

| field | required | notes |
|---|---|---|
| `title` | yes | not persisted by upload-service (see below) — accepted for forward compat with the future catalog-service event |
| `description` | no | same as above |
| `tags` | no | repeated form field, e.g. multiple `tags=` parts; same as above |
| `content_file` | yes | must be `video/*` or `audio/*` |
| `thumbnail_file` | no | must be `image/*` if provided; a shared default is used if omitted |

Response `201` body (also `GET /uploads/{upload_id}` → `200`):

```json
{
  "upload_id": "uuid",
  "content": {
    "s3_bucket": "upload-service-media",
    "s3_key": "{upload_id}/content/{original_filename}",
    "content_type": "video/mp4",
    "size_bytes": 12345,
    "original_filename": "movie.mp4"
  },
  "thumbnail": {
    "s3_bucket": "upload-service-media",
    "s3_key": "{upload_id}/thumbnail/{original_filename}",
    "content_type": "image/jpeg",
    "is_default": false
  },
  "status": "stored",
  "created_at": "2026-08-27T21:18:27.961090Z"
}
```

Note `title`/`description`/`tags` are **required/accepted on the request but
absent from the response** — see "Data ownership" below for why.

Errors: `400` (bad content-type or file too large), `404` (unknown
`upload_id`), `422` (missing required field — FastAPI's own validation),
`502` (S3/DynamoDB write failed after validation passed).

## Data ownership decision (relevant to catalog-service's design)

Per root `CLAUDE.md`'s Inter-Service Communication Convention, upload-service
owns *"the ingestion record only (what was uploaded, where the blobs live,
upload status)"*; catalog-service will own *"the browsable/searchable view
(title, tags, ...)"*.

Concretely: upload-service's own DynamoDB table (`upload-service-uploads`,
partition key `upload_id`, string) stores **only** `upload_id`, the content
asset (bucket/key/content-type/size/filename), the thumbnail asset
(bucket/key/content-type/is_default), `status`, `created_at`. It does
**not** store `title`, `description`, or `tags` — those are accepted as
request input (because `POST /uploads` is currently the only ingress for
them) but are dropped after validation, not persisted, not returned.

**catalog-service should not expect to read any of upload-service's table.**
There is no synchronous call and no shared table — database-per-service is
strict here. `title`/`description`/`tags` reach catalog-service only via the
`upload-events` SQS event described below.

## Thumbnails

Every upload always has a real, usable thumbnail S3 reference — either the
one the client uploaded, or a shared placeholder lazily seeded (once) at a
well-known key (`default_thumbnail_s3_key` config, default
`_defaults/default-thumbnail.png`) in the same bucket. `thumbnail.is_default`
tells you which. **Never delete the object at that shared key** — it's
reused across every upload that didn't supply its own thumbnail. (The
bundled placeholder asset is currently a 1×1 stand-in —
`src/upload_service/assets/default_thumbnail.png` — swap it for a real
placeholder graphic whenever convenient.)

## Built: SNS fan-out → catalog-service, transcoding-service

Both sides are now built for catalog-service, and their contract is verified
to match exactly (field names, nesting — checked against catalog-service's
own `_from_upload_event()` directly, and via a `moto`-mocked cross-check).
transcoding-service's queue now exists too (see below) but nothing consumes
it yet — its worker is still unbuilt.

On successful upload, `services/uploads.py` →
`_publish_upload_created_event()` publishes one JSON message to the
`upload-events` **SNS topic** (`sns_topic_arn` config, default
`arn:aws:sns:eu-central-1:000000000000:upload-events`) — not directly to a
queue anymore. This changed from a direct SQS `send_message` because a
second consumer (transcoding-service) needed the same event, and one SQS
queue can't deliver a message to two independent consumers. catalog-service
and transcoding-service each have their own SQS queue
(`upload-events`/`upload-events-dlq`, `transcoding-jobs`/`transcoding-jobs-dlq`)
subscribed to the topic with `RawMessageDelivery=true`, so each queue's
message Body is still exactly this payload — catalog-service's consumer code
needed **zero changes** for this migration:

```json
{
  "event_type": "upload.created",
  "upload_id": "uuid",
  "title": "...",
  "description": "...",
  "tags": ["..."],
  "content": {"s3_bucket": "...", "s3_key": "...", "content_type": "video/mp4"},
  "thumbnail": {"s3_bucket": "...", "s3_key": "...", "content_type": "image/jpeg"},
  "created_at": "2026-08-27T22:29:03.692547+00:00"
}
```

`content`/`thumbnail` are trimmed to just the fields catalog-service reads
(no `size_bytes`/`original_filename`/`is_default` — those stay
upload-service-only).

**Decided:**
- **Fail-open.** If `publish` itself fails (`get_sns_client()`,
  `clients/aws.py`), it's logged and counted
  (`upload_events_published_total{status="failed"}`) but swallowed — the
  upload response is unaffected. This is the intended "silent dependency
  failure" chaos scenario, not a hard coupling.
- No event envelope versioning yet beyond the `event_type` field — revisit
  if/when the payload shape needs to change in a breaking way.
- Topic name `upload-events` reuses the pre-SNS queue name on purpose, so
  catalog-service's queue name/URL/code stayed unchanged across the
  migration.

## For whoever updates `vod-infra/docker-compose.yml`

Already wired up in `vod-infra/docker-compose.yml` / `vod-infra/localstack-init/
init-aws.sh` as of this writing:

- `upload-service`, built from `vod-upload-service/Dockerfile`, exposing `8000`
  (API) and `9464` (Prometheus scrape port).
- `localstack/localstack` with `SERVICES=s3,dynamodb,sqs,sns`. Its init
  script provisions: S3 bucket `upload-service-media`; DynamoDB tables
  `upload-service-uploads` and `catalog-service-entries` (both keyed on
  `upload_id`, String); the `upload-events` SNS topic; and its two
  subscriber SQS queues — `upload-events`/`upload-events-dlq` (redrive
  `maxReceiveCount: 5`, `VisibilityTimeout` 30s, for catalog-service) and
  `transcoding-jobs`/`transcoding-jobs-dlq` (redrive `maxReceiveCount: 3`,
  `VisibilityTimeout` 900s — generous because a transcode job runs for
  minutes, for transcoding-service, not yet consumed by anything).
- `catalog-service` and `catalog-service-worker` (separate container, same
  image, `command: ["python", "-m", "catalog_service.worker"]`), both
  pointed at the same `upload-events` queue — unaffected by the SNS
  migration, since the queue's name/URL/message shape didn't change.

**Now also wired:** a `jaeger` container (`jaegertracing/all-in-one`, OTLP
receiver enabled, UI at `http://localhost:16686`) — trace export from every
service works end to end. **Still missing:** Prometheus/Grafana containers —
each service's metrics port is exposed to the host (`upload-service:9464`,
`catalog-service:9465`, `catalog-service-worker:9466`) but nothing scrapes
them yet.

Full current env var list is in `vod-upload-service/.env.example`.
