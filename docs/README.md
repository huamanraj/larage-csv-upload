# Contact CSV import: design docs

These docs support one decision: **how we import contact CSVs of up to 1M rows (~250 MB) into a campaign, reliably and without hurting the rest of the product.**

The repo already holds a working implementation of the proposed design (`app/`). Numbers quoted here were measured on that code unless marked *estimate*.

## Read in this order

| # | Doc | What it answers |
|---|---|---|
| 1 | [Chosen approach](01-chosen-approach.md) | What we're proposing and why each decision was made |
| 2 | [Downsides and risks](02-downsides-and-risks.md) | Where it's weak, what breaks first, and how we'd mitigate it |
| 3 | [Alternatives compared](03-alternatives-comparison.md) | A side-by-side matrix of every option we considered |
| 4 | [Debate guide](04-debate-guide.md) | Likely objections with answers, decision criteria, and when to revisit |

### Alternatives, in detail

| # | Alternative | One-line verdict |
|---|---|---|
| A1 | [Parse inside the request (pandas)](alternatives/01-in-request-pandas.md) | The baseline to avoid; it fails at our file sizes |
| A2 | [Celery + Redis/RabbitMQ](alternatives/02-celery-redis.md) | Works, but adds two moving parts and makes exactly-once harder |
| A3 | [Postgres queue library (Procrastinate / PGMQ / pg-boss)](alternatives/03-postgres-queue-library.md) | Same idea as ours, less hand-rolled code; a strong contender |
| A4 | [Object storage + serverless (S3 presigned + SQS/Lambda)](alternatives/04-object-storage-serverless.md) | Best path once we need multi-node or resumable uploads |
| A5 | [In-database ELT (COPY raw, validate in SQL)](alternatives/05-in-database-elt.md) | The fastest raw path, but pushes logic into SQL |
| A6 | [DuckDB / Polars vectorised processing](alternatives/06-duckdb-polars.md) | An upgrade to the worker's internals, not a replacement for the design |
| A7 | [Client-side parsing (browser)](alternatives/07-client-side-parsing.md) | Cuts upload size a lot, but the tab has to stay open |
| A8 | [Workflow engine (Temporal)](alternatives/08-workflow-engine-temporal.md) | Excellent durability, but heavy infrastructure for one pipeline |
| A9 | [Streaming platform (Kafka)](alternatives/09-streaming-kafka.md) | Overkill; this is a batch problem |
| A10 | [Embedded importer SaaS (Flatfile / OneSchema-style)](alternatives/10-embedded-importer-saas.md) | Fastest to ship a mapping UI, but PII leaves our infra and it costs per use |

## TL;DR

- **Proposal:** a single box running `api` + `worker` + Postgres. Postgres is the job queue (`SKIP LOCKED`). Files stream to disk and are processed in 10k-row chunks, and each chunk is one `COPY` + merge + checkpoint transaction.
- **Why:** no new infrastructure, **exactly-once and resumable** by construction, constant memory, and measured at about 26k rows/s end to end on 4 cores.
- **Main costs:**
  - It's one box, with local disk and no horizontal scale.
  - Upload goes through our API with no resume support.
  - One global import slot means big files block small ones.
  - Campaign-wide dedupe is "first row wins".
- **When to move on:** once we need more than one app node, flaky-network uploads, or more than ~10 concurrent large imports. The planned next step is A4 for upload and storage, and possibly A3 for the queue. The chunk transaction stays the same.
