# A4. Object storage + direct upload + queue/serverless workers

> **Verdict:** the right *next* step, not the right *first* step. It fixes our two biggest weaknesses: uploads that can't resume, and files on local disk tying us to one box. It keeps the same processing core, at the cost of cloud components and more moving parts.

## How it works

```
browser ─(1) POST /imports/init ─▶ api: create imports row, return presigned multipart URLs
browser ─(2) PUT parts (5–100 MB each, parallel, retry per part) ─▶ S3 / GCS / MinIO bucket
browser ─(3) POST /imports/{id}/complete ─▶ api: CompleteMultipartUpload, read ETag/size
                                                     │
      (4) preview: ranged GET of the first ~1 MB ◀───┘
      (5) mapping confirmed → status=queued (+ optional SQS message / S3 event)
                                                     │
worker (VM / ECS task / Lambda / Cloud Run job) ◀────┘
      streams s3.get_object(Body) → csv reader → same chunk transaction into Postgres
```

There are two variants of the trigger:
- **Keep the Postgres queue.** The worker still claims with `SKIP LOCKED` and just reads from S3. This is the least change.
- **Cloud-native trigger.** S3 event → SQS → Lambda or Fargate task. The Postgres checkpoint still guards exactly-once.

## Sketch

```python
# api
mpu = s3.create_multipart_upload(Bucket=B, Key=f"imports/{id}.csv", ContentType="text/csv")
urls = [s3.generate_presigned_url("upload_part", Params={..., "PartNumber": n, "UploadId": mpu["UploadId"]},
        ExpiresIn=3600) for n in range(1, parts + 1)]

# worker: only the file-open line changes
body = s3.get_object(Bucket=B, Key=imp.key)["Body"]
reader = csv.reader(io.TextIOWrapper(body, encoding="utf-8-sig", newline=""))
```

**Idempotency:** a client-side sha256, or the S3 checksum (`x-amz-checksum-sha256`), replaces the server-side hash. Keep `UNIQUE (campaign_id, file_sha256)`.

## Pros

- **Resumable, parallel uploads.** A network blip retries one part, not 250 MB. This is a big UX win for slow or mobile users.
- **The API never touches file bytes.** There are no proxy body limits or long-held connections.
- **Workers are stateless and can run anywhere.** This enables horizontal scale and autoscaling on queue depth.
- Storage has lifecycle rules (auto-delete raw CSVs after N days), versioning and encryption at rest for free.
- With the Lambda or Cloud Run variant, compute cost is zero when idle.

## Cons

- **More components:** a bucket, IAM policies, CORS on the bucket, presign endpoints, maybe SQS and a DLQ, and a local emulator (MinIO/LocalStack) for development and tests.
- **Server-side hashing is gone.** You either trust the client's hash or have the worker hash while it processes (a duplicate is then detected late).
- **Lambda specifics:**
  - The 15-minute max runtime is fine for 1M rows (about 1 min), but a pathological file or a slow database could hit it.
  - Cold starts.
  - Postgres connection storms under high concurrency, which needs RDS Proxy or PgBouncer.
- **Preview** needs a ranged GET, and the file must be readable before mapping (it is, after `complete`).
- **Cloud lock-in** in the upload protocol (S3-compatible APIs mitigate this: MinIO, R2, GCS interop).
- **Data residency:** the bucket region must satisfy PII rules. This is a solvable configuration choice.

## Cost

- Storage is cents per GB-month.
- Requests and egress between the bucket and a worker in the same region are small.
- Lambda or Fargate is billed per second of processing. At a few imports per hour, all of this is negligible.

## When to choose it

- We run more than one API or worker node.
- Upload failures or timeouts are a real support issue.
- We want autoscaling or scale-to-zero workers.
- We're already on AWS or GCP and comfortable with IAM.

## Migration from the chosen design

It's incremental:
1. Add init/complete endpoints and presigned URLs. Keep the old streaming endpoint as a fallback.
2. Store `file_key` instead of `file_path`. The worker opens S3 instead of the local disk.
3. Replace the retention sweep with a bucket lifecycle rule.
4. Optionally move the worker to containers or Lambda and trigger it via SQS. Keep the Postgres checkpoint and ownership guard.

The chunk transaction, `contacts`, dedupe, the UI progress and the fetch endpoints are **unchanged**.
