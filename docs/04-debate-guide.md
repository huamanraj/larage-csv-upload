# 4. Debate guide

This guide is for the design review. Each objection below is one an engineer is likely to raise, followed by a short answer and where the evidence lives. The aim is to agree on the **decision criteria** first, then check each option against them.

## Agree on the criteria first

Rank these as a team before arguing about tools. Most disagreements are really disagreements about this ranking.

| Criterion | Our proposed weight | Why |
|---|---|---|
| Correctness: no lost or duplicated contacts, even on crash | **Must** | Duplicates mean double-dialling customers, and lost rows mean lost revenue |
| API stays responsive during imports | **Must** | Imports must not degrade calling operations |
| Ops burden (new stateful systems) | High | Small team; every new service needs on-call knowledge |
| Time to ship | High | Imports are blocking campaign onboarding |
| PII stays in our infrastructure | High | Phone numbers plus names are personal data |
| Throughput (1M rows under ~1–2 min) | Medium | Users wait minutes for campaigns anyway |
| Horizontal scale | Low today | One box handles projected volume; revisit on triggers |
| Resumable uploads | Medium | Depends on who uploads: office users are fine, field teams are not |

## Likely objections, with answers

### "Postgres isn't a queue. Use Redis or RabbitMQ."
- The queue holds **a few jobs per hour**. The argument against Postgres queues applies to high-rate tiny messages, not this.
- The decisive point is **atomicity**. Our "job advanced to row N" update commits in the *same transaction* as rows 1..N. With an external broker, the ack and the DB commit are two systems, so we'd need an outbox or idempotency keys to get the same guarantee. See [A2](alternatives/02-celery-redis.md#exactly-once-is-the-hard-part).
- `FOR UPDATE SKIP LOCKED` is the standard primitive that Postgres queue libraries (Procrastinate, PGMQ, pg-boss, River, Oban, graphile-worker) are built on.

### "We'll need Celery eventually anyway."
- Maybe, for *other* job types. If we adopt it, the import can be one Celery task that runs the same chunk loop. The chunk transaction and the checkpoint are unchanged, so nothing here is wasted.

### "Why not just pandas `read_csv` in a background thread?"
- It loads the whole 250 MB file, and pandas' memory use for object columns is often 5–10× the file size (*estimate*). That's 1–2.5 GB per import, in the API process, fighting the GIL. See [A1](alternatives/01-in-request-pandas.md).

### "Why not load the raw file with COPY and do everything in SQL? That's faster."
- It's true that raw load is fastest ([A5](alternatives/05-in-database-elt.md)). But:
  - There's no `phonenumbers` fallback in SQL.
  - All 93 columns land in a staging table.
  - Progress and crash resumption get coarse unless we batch it ourselves, which recreates our chunk loop.
- Our measured bottleneck is the **merge into the unique index**, which A5 pays too. So the speed win is smaller than it looks.

### "Uploading 250 MB through our API is bad practice."
- Agreed at scale. It's the first thing we'd change, and it's listed as risk #2.
- Today it's streamed (64 KB at a time), capped, hashed, and idempotent on retry.
- The upgrade path, presigned multipart upload to object storage ([A4](alternatives/04-object-storage-serverless.md)), replaces only the upload endpoint and the file-open call in the worker.

### "Single box? What about HA?"
- The import pipeline adds **no new single point of failure**: it uses the same Postgres and host the product already runs on.
- A crash is **recoverable without human action**. The stale lock is re-claimed and the job resumes at its checkpoint (tested with `kill -9`).

### "10k rows per chunk is arbitrary."
- It's tunable (`CHUNK_SIZE`).
- **Bigger chunks** mean fewer transactions but a larger redo on crash, higher RSS, and longer lock hold times.
- **Smaller chunks** mean more per-transaction overhead.
- Measured at 10k: RSS ~48 MB flat, about 0.3–0.5 s per chunk, so a crash redoes under a second of work.

### "`synchronous_commit = off` is dangerous."
- Only if the checkpoint lived somewhere else. It commits in the same transaction as the data, so after a Postgres crash both are lost together and the chunk is redone.

### "The hand-rolled claim loop will have bugs."
- It's about 120 lines. The two dangerous bugs are double processing and lost jobs, and they're covered by:
  - the ownership guard (`worker_id` plus expected `checkpoint_row`)
  - the `kill -9` resume test
- If the team prefers a library, [A3](alternatives/03-postgres-queue-library.md) is a drop-in replacement for the claim loop.

### "What about just buying an importer (Flatfile, OneSchema)?"
- It's a valid business choice. The debate is about PII leaving our infrastructure, per-import cost, and plan row limits. See [A10](alternatives/10-embedded-importer-saas.md).
- We still need the server-side merge and dedupe into `contacts`, so it replaces the UI and validation, not the pipeline.

### "Can't the browser parse the CSV and send only the 5 columns we need?"
- Yes, and it's a good idea: the upload shrinks from about 250 MB to about 30–40 MB (*estimate*). See [A7](alternatives/07-client-side-parsing.md).
- The costs:
  - The tab has to stay open for the whole import.
  - Slow laptops parse slowly.
  - We must still re-validate on the server.
- It's a strong candidate if uploads, rather than processing, turn out to be the pain point.

## Questions the team should answer (they change the design)

1. **Duplicate policy:** does a later upload update the name and vars of a not-yet-called contact, or keep the first? This decides between `DO NOTHING` and `DO UPDATE`.
2. **Who uploads, from where?** Field teams on mobile data push toward resumable uploads (A4 or A7) sooner.
3. **Expected concurrency:** how many large imports run at the same time at peak? More than about 3 moves the horizontal-scale trigger closer.
4. **Retention and compliance:** how long may we keep the raw CSV? (Currently 7 days, and rejects are kept.) Do contracts forbid third-party processors? That rules A10 in or out.
5. **Cross-campaign dedupe or DNC lists:** are they needed? That's an extra anti-join in the merge; it doesn't change the architecture.

## Triggers to revisit the decision

Agree on these now so the future conversation is short:

| Trigger | Move to |
|---|---|
| We run the API on more than one node | Object storage for files ([A4](alternatives/04-object-storage-serverless.md)); the queue can stay in Postgres |
| More than 5% of uploads fail or time out, or users on mobile networks | Presigned multipart ([A4](alternatives/04-object-storage-serverless.md)) or client-side parsing ([A7](alternatives/07-client-side-parsing.md)) |
| p95 wait in `queued` exceeds 2 minutes | Size-aware priority, then more slots, then more worker nodes |
| We add 3 or more other background job types | Adopt a Postgres queue library ([A3](alternatives/03-postgres-queue-library.md)) or Celery ([A2](alternatives/02-celery-redis.md)) for all of them |
| Validation CPU above ~50% of chunk time at our volumes | Vectorised normalisation ([A6](alternatives/06-duckdb-polars.md)) |
| `contacts` over ~100M rows and the merge slowing down | Partition `contacts` by campaign |
| Multi-step flows (import → enrich → DNC check → schedule) with retries and human steps | A workflow engine ([A8](alternatives/08-workflow-engine-temporal.md)) |

## Suggested review agenda (45 min)

1. **(5 min)** Problem and requirements: [01](01-chosen-approach.md#problem-statement).
2. **(10 min)** Live demo: upload a 300k file, then `kill -9` the worker mid-run and watch it resume.
3. **(10 min)** Rank the criteria (table above).
4. **(15 min)** Objections, using the matrix in [03](03-alternatives-comparison.md).
5. **(5 min)** Decide, and assign the pre-launch fixes from [02](02-downsides-and-risks.md).
