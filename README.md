# Large CSV contact import

FastAPI + psycopg3 import pipeline for contact CSVs of up to about 1M rows, with a built-in UI that shows each stage as it runs.

One server runs three processes: `api` (uvicorn), `worker` (its own process with a small process pool), and Postgres. There is no Redis or Celery: Postgres is the queue, and `FOR UPDATE SKIP LOCKED` hands each worker the next free job.

## Run

```bash
docker compose up --build
# open http://localhost:8000
```

Without Docker (you need a reachable Postgres):

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres@127.0.0.1:5432/csvimport ./run.sh
```

Make test data (Outlook-style, with messy phones, Excel `9.19E+11` values, landlines and duplicates):

```bash
python scripts/make_sample.py 1000000 contacts_1m.csv
```

To slow a demo down so the chunk animation is easy to follow, set `CHUNK_DELAY_MS=150` on the worker.

## Flow

| # | Stage | Where |
|---|---|---|
| 1 | `POST /api/imports?campaign_id=` returns `202 {import_id}`. The raw body goes from `request.stream()` straight to disk, with the sha256 computed on the fly. A size cap is enforced, and `UNIQUE (campaign_id, file_sha256)` makes a re-upload idempotent: it returns the existing id. | `app/main.py` |
| 2 | `GET /api/imports/{id}/preview` reads the first 50 rows and scores each column as a phone column (header name match plus the share of valid sampled values). `POST /api/imports/{id}/mapping` takes the phone **priority list**, the name column, the `vars` columns, the region (`IN` by default) and the optional landline rejection, and sets the status to `queued`. | `app/main.py`, `app/phones.py` |
| 3 | The worker claims a job with `SKIP LOCKED` and sets `locked_at`. An advisory lock serialises claims, so the global `IMPORT_CONCURRENCY` limit holds across worker processes. A lock older than `LOCK_TIMEOUT` is re-claimed. | `app/worker.py` |
| 4 | A streaming CSV reader cuts the file into 10k-row chunks. Each chunk is validated across a `ProcessPoolExecutor` sized to cores − 1. The fast path `^(?:91\|0)?([6-9]\d{9})$` handles most Indian numbers, and `phonenumbers` handles the rest. Rejection reasons are `empty`, `invalid`, `sci_notation` and `landline`. | `app/worker.py`, `app/phones.py` |
| 5 | Each chunk is one transaction: `COPY` into the temp `stage` table, `COPY` into `import_errors`, merge with `DISTINCT ON` + `ON CONFLICT DO NOTHING`, then update the checkpoint and counters. The checkpoint commits with the data, so a restart skips rows `<= checkpoint_row` and nothing is processed twice. | `app/worker.py` |
| 6 | Contacts use keyset pagination: `GET /api/campaigns/{cid}/contacts?status=&after_id=`. Rejected rows use `GET /api/imports/{id}/errors?after_row=`. Both exports stream through `COPY TO STDOUT` (`…/contacts.csv`, `…/errors.csv`). Counters are read from `imports`, never from `COUNT(*)`. | `app/main.py` |

The UI polls `GET /api/imports/{id}?after_event=` every second. Each chunk transaction also writes one row to `import_events`, and the UI plays those rows back as the chunk grid, the per-chunk transaction lane, and the throughput and worker-memory lines.

### Guarantees
- **Exactly-once / resumable.** A chunk first runs `UPDATE imports … WHERE worker_id = me AND checkpoint_row = expected`. A worker whose lock went stale and was taken over cannot commit a duplicate chunk.
- **Graceful stop.** On SIGTERM the worker finishes its current chunk and returns the job to `queued`.
- **Constant memory.** Worker RSS depends on the chunk size, not the file size. The UI plots it for every chunk.
- **Retention.** The worker deletes a raw CSV `RETENTION_DAYS` after its import finishes. `import_errors` is kept, so the rejected-rows CSV stays downloadable.
- `ANALYZE contacts` runs after any import larger than `ANALYZE_AFTER_ROWS`.

### Deviations from the design
- Rows are read with `csv.reader` and mapped columns are picked by index. It is the same streaming approach as `csv.DictReader`, but it skips building a 93-key dict for every row and handles duplicate header names.
- Some Indian STD codes (080, 079, …) also match the mobile fast path. When landline rejection is on, fast-path numbers still get a `number_type` check, run on a prebuilt number with no `parse()` call.

## Measured on 4 cores (3 validators)

| File | Rows | Wall time | Worker RSS |
|---|---|---|---|
| 13 MB | 100k | 3.3 s | ~48 MB |
| 40 MB | 300k | 11.5 s | ~49 MB |

A `kill -9` of the worker at row 60,000, followed by a restart, produced exactly the same counters as a clean run (189,190 new / 81,233 duplicate / 29,577 rejected).

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres@127.0.0.1:5432/csvimport` | Postgres connection string |
| `DATA_DIR` | `/data/imports` | where uploaded CSVs are stored |
| `MAX_UPLOAD_MB` | `300` | upload size cap |
| `CHUNK_SIZE` | `10000` | rows per chunk and per transaction |
| `IMPORT_CONCURRENCY` | `1` | global limit on running imports |
| `POOL_SIZE` | cores − 1 | validation processes |
| `LOCK_TIMEOUT` | `10 min` | age after which a lock counts as stale |
| `RETENTION_DAYS` | `7` | days before a raw CSV is deleted |
| `CHUNK_DELAY_MS` | `0` | pause between chunks (demo only) |

Postgres settings for the box (see `docker-compose.yml`): `shared_buffers` at about 25% of RAM, and `max_wal_size=4GB`.

## Design docs

The justification, downsides, alternatives, a debate guide and Eraser architecture diagrams are in [`docs/`](docs/README.md) and [`docs/diagrams/`](docs/diagrams/README.md).
