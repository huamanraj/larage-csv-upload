# 1. The chosen approach, and why

## Problem statement

Users upload contact lists, usually exported from Outlook or Excel, to feed a calling campaign.

**The input:**
- Up to **~1M rows / ~250 MB** per file.
- Often **~93 columns**, of which we need 3–6.
- Phone numbers are messy: `+91 98765 43210`, `098765 43210`, `91-9876543210`, `(987) 654-3210`, Excel's `9.19877E+11`, landlines, blanks, foreign numbers.
- Duplicates occur both within a file and across files in the same campaign.

**Requirements:**
1. The API must stay responsive during an import.
2. No row is lost and no row is imported twice, even if a process crashes mid-import.
3. Users see progress and get their rejected rows back with a reason.
4. Output: one `contacts` row per unique `(campaign, E.164 phone)`, with `status='pending'`. This is the hand-off to the calling layer.
5. Must run on **one server** we already operate. The team is small, and ops time is the scarcest resource.

## The design in one picture

```
 browser ──PUT raw bytes──▶ api (uvicorn)
                              │  stream to /data/imports/{id}.csv, sha256 on the fly
                              │  INSERT imports (status=uploaded)   UNIQUE(campaign, sha256)
                              ▼
 browser ◀─ headers + sample + phone-column scores ─ GET /preview (first 50 rows only)
 browser ──mapping──▶ POST /mapping  → status=queued
                              │
          Postgres  ◀─────────┘   (the queue is just the imports table)
              ▲
              │ UPDATE … WHERE id=(SELECT … FOR UPDATE SKIP LOCKED) → status=processing, locked_at=now()
              │
          worker process ── csv stream ─▶ 10k-row chunk ─▶ ProcessPool (cores-1) validate
              │                                                   │
              └──────────── ONE transaction per chunk ◀───────────┘
                   COPY stage ▸ COPY import_errors ▸ INSERT … DISTINCT ON … ON CONFLICT DO NOTHING
                   ▸ UPDATE imports SET checkpoint_row, counters, locked_at   (commit together)
```

## Decisions and the reasoning behind each

### D1. Postgres is the queue (no Redis/Celery)

- The job state (`imports` row) and the job's output (`contacts`) live in the **same database**. That lets the "job progressed" update commit **atomically** with the data it describes. No broker can give us that without a two-phase commit or an outbox.
- `SELECT … FOR UPDATE SKIP LOCKED` is exactly the primitive a work queue needs. Concurrent workers skip rows another transaction has locked instead of blocking, so each one picks up a different job.
- Queue volume is tiny: a few imports per hour, not thousands of messages per second. Postgres handles this with essentially zero load.
- It's one less stateful service to deploy, monitor, back up, secure and upgrade.

### D2. Stream the upload to disk and compute sha256 on the fly

- The file is never held in RAM and never parsed in the request. A 250 MB upload costs about 64 KB of memory at a time.
- The hash is free (computed while bytes pass through), and `UNIQUE (campaign_id, file_sha256)` makes a **double-click, retry or re-upload idempotent**: it returns the existing import id.
- Newlines are counted during the same pass to give the progress bar an estimated row total, without a second read.

### D3. Preview reads only the first 50 rows, and a human confirms the mapping

- Auto-detection gets it right most of the time. Each column is scored as `0.4 × header-name match + 0.6 × share of sampled values that are valid phones`. Wrong guesses are expensive, though: a whole campaign could dial the wrong column. So the user confirms.
- A **phone priority list** (`Mobile Phone → Primary Phone → Business Phone`, first valid one wins) recovers rows where the preferred column is empty. In our synthetic sample, about 5% of rows had an empty `Mobile Phone` but a valid `Primary Phone`. Real Outlook exports tend to be worse.
- Only the chosen columns are kept as `vars` (jsonb), **not all 93**. This controls storage and PII exposure.

### D4. Separate worker process with a process pool (cores − 1)

- Validation is CPU-bound Python. Running it in the API process would starve request handling. Threads wouldn't help because of the GIL.
- One pool slot is left free for the API and Postgres.
- A global limit of **1–2 concurrent imports** (enforced across worker processes with an advisory lock around the claim) keeps a 1M-row file from starving everything else.

### D5. Fast path before `phonenumbers`

- The steps are: strip non-digits, then match `^(?:91|0)?([6-9]\d{9})$`, which gives `+91XXXXXXXXXX` directly.
- In our realistic sample, **99% of valid rows took the fast path**.
- The measured fast path runs at about 350k rows/s per core, against about 25k rows/s for `phonenumbers.parse`. Only the leftovers (foreign numbers, odd formats) pay the slow cost.
- Rejections carry a reason: `empty`, `invalid`, `sci_notation` (unrecoverable, because Excel already dropped the digits) and `landline` (optional).

### D6. Ten-thousand-row chunks, each one transaction: COPY → merge → checkpoint

This is the heart of the design.

```sql
SET LOCAL synchronous_commit = off;
UPDATE imports SET locked_at=now() WHERE id=$id AND worker_id=$me AND checkpoint_row=$expected;  -- ownership guard
COPY stage (...) FROM STDIN;                    -- temp table, ON COMMIT DELETE ROWS
COPY import_errors (...) FROM STDIN;
INSERT INTO contacts (...) SELECT … FROM (SELECT DISTINCT ON (phone_e164) … ORDER BY phone_e164, row_no) …
  ON CONFLICT (campaign_id, phone_e164) DO NOTHING;   -- DB enforces dedupe
UPDATE imports SET checkpoint_row=$last, counters += … ;
COMMIT;
```

- **Exactly-once and resumable.** The checkpoint commits with the data. After a crash, a new worker re-claims the job and skips rows `<= checkpoint_row`. A half-done chunk rolled back entirely, so nothing is duplicated and nothing is lost.
- **Split-brain safe.** The ownership guard (`worker_id = me AND checkpoint_row = expected`) means a worker that was presumed dead and replaced cannot commit a stale chunk.
- **COPY instead of INSERT.** Bulk COPY is 5–10× faster than batched INSERTs, and very much faster than ORM row-by-row inserts.
- **`synchronous_commit = off` is safe here.** If Postgres crashes, the last few commits might be lost, but the checkpoint is lost together with them, so the worker simply redoes those chunks.
- **Dedupe is enforced by the database.** `UNIQUE (campaign_id, phone_e164)` handles duplicates across chunks and across files, and `DISTINCT ON` handles duplicates within a chunk. There's no in-memory set that grows with the file.
- **Why 10k per chunk:** it's small enough that RSS stays flat (~48 MB measured) and a crash loses at most a couple of seconds of work. It's big enough that per-transaction overhead is negligible.

### D7. Fetch: keyset pagination, stored counters, and a streamed export

- `WHERE campaign_id=$1 AND id > $cursor ORDER BY id LIMIT 50` stays constant-time on page 20,000, whereas `OFFSET` gets slower on every page.
- Counters are read from the `imports` row that the chunk transaction maintains. We never run `COUNT(*)` over millions of contacts.
- Exports use `COPY (…) TO STDOUT`, streamed to the client, so the server never builds the CSV in memory.

### D8. Progress through a per-chunk event row, polled by the UI

- Each chunk transaction also inserts one `import_events` row with its timings and counts. Because it's in the same transaction, the UI **can never show progress that didn't commit**.
- Polling every 1–2 s is simpler than SSE or WebSockets, survives proxies, and costs one indexed query per client.

## Measured results (4-core VM, 3 validator processes, local Postgres 16)

| File | Rows | End-to-end | Throughput | Worker RSS |
|---|---|---|---|---|
| 13 MB | 100k | 3.3 s | ~30k rows/s | ~48 MB |
| 40 MB | 300k | 11.5 s | ~26k rows/s | ~49 MB (flat) |
| 250 MB | 1M | *~40 s estimate* (linear extrapolation) | | ~50 MB *estimate* |

Per-chunk breakdown (typical, 10k rows):

| Step | Time |
|---|---|
| read | 40–60 ms |
| validate | 30–50 ms |
| COPY stage | 10–25 ms |
| COPY errors | 4–10 ms |
| merge | 100–200 ms |

**The merge (unique-index maintenance) is the bottleneck, not Python.**

**Crash test:** we ran `kill -9` on the worker (and its pool) at row 60,000 of 300,000, then restarted. The import resumed at row 60,001 and finished with counters **identical** to a clean run: 189,190 new / 81,233 duplicate / 29,577 rejected.

## What we deliberately did *not* build

- Auth and multi-tenancy (assumed to come from the host app).
- Resumable uploads. See [downsides](02-downsides-and-risks.md) and [A4](alternatives/04-object-storage-serverless.md).
- XLSX input. Users export to CSV.
- Updating existing contacts on conflict. The current rule is "first row wins", and changing it is a one-line `DO UPDATE` if the product wants it.

## Evolution path (no rewrite needed)

1. **Now:** single box as described.
2. **More import load:** raise `IMPORT_CONCURRENCY` to 2, and add a size-aware priority so small files jump ahead of 1M-row ones.
3. **Multi-node or flaky networks:**
   - Move uploads to object storage with presigned multipart uploads ([A4](alternatives/04-object-storage-serverless.md)). Workers read from there.
   - The queue and the chunk transaction stay the same.
4. **Want less hand-rolled queue code:** swap the claim loop for a Postgres queue library ([A3](alternatives/03-postgres-queue-library.md)). This is still one database.
5. **CPU-bound at huge volumes:** swap the per-row Python validation for DuckDB/Polars vectorised normalisation inside the same worker ([A6](alternatives/06-duckdb-polars.md)).
