# Large CSV contact import (POC)

Upload a contacts CSV. The server streams it to local storage and queues it in Postgres. A worker streams it back in 10k-row chunks, validates each chunk and saves it. The UI replays every step at any speed, and you can drag the timeline back to any moment.

Three processes run on one server: `api` (uvicorn), `worker` (its own process with a small process pool) and Postgres. There is no Redis or Celery: Postgres is the queue.

## CSV schema

Two layouts are accepted as they are, with no mapping step (see `app/columns.py`):

**Simple**

| Column | Required | Notes |
|---|---|---|
| `phone` | yes | any country: `+44 7911 123456`, `0044…`, `07911 123456` (with `country`), `98765 43210` |
| `name` | no | |
| `country` | no | `India`, `IN`, `UK`, `USA`, `+44`, … Used for numbers written without a country code. If it's empty, `DEFAULT_REGION` is used |
| `email` | no | checked: a malformed address is blanked (the row is still imported) |
| `birthday` | no | checked: stored as `YYYY-MM-DD`; placeholders (`0/0/00`) and impossible dates (31 Feb) are blanked |
| anything else | no | kept in `contacts.vars` (jsonb), non-empty values only |

**Outlook contacts export** (the standard ~92-column file)

- **Phone:** the first valid number among `Mobile Phone → Number → Primary Phone → Business Phone → Home Phone → …` wins. Fax, pager and ID-number columns are never used.
- **Name:** `First Name + Middle Name + Last Name`.
- **Country:** `Home Country/Region`, else `Business Country/Region`.
- **Everything else** (company, e-mail, address, …) goes into `vars`.

Header names are case-insensitive. A file with no usable phone column is rejected with a 422 error. A row whose numbers are all invalid ends up in `import_errors` with its reason. For example, Outlook's placeholder `555-555-1212` is not a real number, so it's rejected.

## Validation

Per row, cheapest check first. A row is rejected only when it has no usable phone number:

| Check | Result | Reason in `import_errors` |
|---|---|---|
| Phone cell empty | rejected | `empty` |
| Excel scientific notation (`9.19E+11`): the digits are already lost | rejected | `sci_notation` |
| Fewer than 5 digits | rejected | `too_short` |
| Not a valid number for its country (`phonenumbers`; Indian mobiles take a regex fast path) | rejected | `invalid` |
| Placeholder: the number ends in 8+ identical digits (`9999999999`, `+91 90000 00000`) | rejected | `junk` |
| Valid | saved as `+E.164` | — |
| Same number twice in the file, or already in the campaign | skipped, first row wins | counted as duplicate |
| E-mail / birthday column malformed | that field is blanked, the row is kept | counted per chunk |

The UI shows the breakdown per import (`invalid 8,279 · empty 6,688 · sci notation 3,222 · too short 313`) and how many e-mails and dates were blanked.

## Run

```bash
docker compose up --build            # open http://localhost:8000
```

Without Docker (needs a reachable Postgres):

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres@127.0.0.1:5432/csvimport ./run.sh
python scripts/make_sample.py 1000000 contacts_1m.csv   # test data: messy + foreign numbers + duplicates
pip install -r requirements-dev.txt && python -m pytest tests
```

## Flow (matches the UI and `docs/diagrams/0-poc-flow.flow.eraser`)

1. **Create import row.** `POST /api/imports` inserts `imports` with `status = 'uploading'`.
2. **Upload stream loop.** The request body is read piece by piece. Each piece is written to `DATA_DIR/{id}.csv` and fed into the sha256, so the file is never held in RAM.
3. **Mark queued.** The row is set to `status = 'queued'`. That *is* the push to the queue. Re-uploading the same file to the same campaign returns the existing import (`UNIQUE (campaign_id, file_sha256)`).
4. **Worker picks up the job.** The claim uses `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`, so two workers never take the same import. A worker that dies leaves a stale `locked_at`, and the job is re-claimed.
5. **Chunk loop.** The file is read with a streaming `csv.reader`, 10k rows at a time:
   - **Validate** across a process pool (cores − 1). Indian mobiles take a regex fast path; every other number goes through `phonenumbers`. The results are `+E.164`, or a rejection reason: `empty`, `sci_notation`, `too_short`, `invalid` or `junk`. E-mail and birthday fields are checked in the same pass.
   - **Save**, in one transaction: `COPY` into a temp table, then `INSERT … SELECT DISTINCT ON (phone) … ON CONFLICT DO NOTHING` into `contacts`, then `COPY` the rejects into `import_errors`.
   - **Checkpoint and progress**, in the same transaction. After a crash the worker skips rows `<= checkpoint_row`, so no row is saved twice or lost.
6. **Done.** The row is set to `status = 'done'`.

Every step writes an `import_events` row. The UI polls `GET /api/imports/{id}` and turns those events into a timeline. Use **0.1×–5×** to slow it down or speed it up, and drag the bar (or press ←/→) to rewind. Press space to play or pause.

**Resource charts.** Three charts under the flow follow the same timeline and cursor:

- **CPU:** cores in use, measured with `psutil`.
  - During upload, the API process.
  - For each chunk, the worker plus its validation processes, split into read, validate and save.
- **RAM:** memory held by the API during upload, and by the worker with its validation processes afterwards. It should stay flat whatever the file size.
- **DB writes:** WAL bytes each chunk's transaction made Postgres write (`pg_current_wal_insert_lsn()` measured before and after), plus the time spent in the database. These are the database's per-chunk write spikes.

The numbers are recorded in the events, so they replay and rewind with everything else. Postgres's own CPU and RAM are not charted, because the worker can't see the database container's processes. Use `docker stats` for that.

**Core cap (latency benchmarking).** Pick **auto / 1 / 2 / 4 / 6 / 8** next to the drop zone before uploading, or press **re-run** in the runs table to process a stored file again with the selected cap.
- **How the cap works:** the worker pins itself and its validation processes to the first N CPUs (Linux `sched_setaffinity`) and validates each chunk in N parallel slices. It gets all CPUs back after the import.
- **Fair comparison:** each re-run goes into a fresh campaign, so every run does identical work. The runs table then shows time and rows/s per cap.
- **What the cap covers:** only the worker. Postgres is not capped, so to limit the whole box, also set `cpus:` on the `db` service in `docker-compose.yml`.
- **Where it applies:** pinning covers the whole worker, so it only happens with `IMPORT_CONCURRENCY=1`. With several parallel slots, the cap only sets the number of validation slices. Under Docker Desktop, the maximum is the number of CPUs given to Docker's VM.

Measured on 4 cores, 100k rows:

| Cap | Mostly Indian numbers | All foreign numbers |
|---|---|---|
| 1 core | 3.2 s | 8.1 s |
| 2 cores | 2.6 s | 4.7 s |
| 4 cores | 2.4 s | 4.0 s |

Extra cores mostly speed up validation. The reading and the database save stay about the same.

Results can be paged with `GET /api/campaigns/{id}/contacts?after_id=` (keyset pagination, no `OFFSET`).

## Parallel imports

Drop several CSV files at once (or pick several in the file dialog). Each upload streams independently, and the worker processes up to `IMPORT_CONCURRENCY` imports at the same time (default 4). Each slot is a thread; all slots share the validation process pool.

- **campaign: own per file** (default): each file gets its own campaign, like files from different customers. They run **in parallel**.
- **campaign: shared**: every file goes into campaign 1. They run **one after another**. Two imports into the same campaign would race on its duplicate check, so the queue never runs them together. The waiting import shows `waiting for #N`.
- The API does the same: `POST /api/imports` without `campaign_id` creates a new campaign; with `campaign_id` it imports into that campaign.

**System panel** (top of the page). One time axis for everything, sampled every 0.5 s:
- **CPU:** stacked per process: worker (with its validation processes), API, Postgres, and the rest of the machine.
- **RAM:** stacked per process (PSS, so shared memory isn't double-counted), plus machine total.
- **DB writes:** WAL MB/s and rows inserted/s, from Postgres's own counters.
- **One bar per import:** a thin line while it uploads or waits, a thick bar while it's processed.

Hover anywhere to read that moment. While imports run the view is live; afterwards it stays on the last run.

Under Docker, Postgres runs in its own container, so its processes aren't visible to the worker. Its CPU then shows as part of **db + other**, the machine total minus the API and worker. That total is the Docker VM's.

Measured on a 4-vCPU VM, everything on one box, 300k rows per file (25 MB). Times run from the first uploaded byte to the last import done:

| Files | Campaigns | Wall time | Total rows/s | Machine CPU avg / peak | Worker RAM | Postgres RAM | API RAM |
|---|---|---|---|---|---|---|---|
| 1 | — | 8.4 s | 36k | 1.7 / 1.8 cores | 154 MB | 380 MB | 60 MB |
| 2 | own | 10.5 s | 57k | 2.9 / 3.7 | 158 MB | 380 MB | 60 MB |
| 4 | own | 19.8 s | 61k | 3.1 / 3.9 | 160 MB | 384 MB | 61 MB |
| 4 | shared (one by one) | 34.6 s | 35k | 1.8 / 2.4 | 162 MB | 383 MB | 61 MB |

What this shows:
- **2 parallel imports nearly double throughput.** One import uses only ~1.2 cores, because it reads, validates and saves in turn.
- **At 4 parallel the 4 cores are full.** Throughput stops rising, and each import takes longer because they share the CPU. Rule of thumb: about 1 parallel import per 2 cores.
- **Memory doesn't grow with the number of imports:** each slot holds one 10k-row chunk.

## Measured (4 cores, 3 validators)

- **100k rows (5 MB):** processed in about 2.8 s.
- **Worker memory:** stays around 45–50 MB whatever the file size, because only one chunk is in memory at a time.
- **Crash test:** a `kill -9` mid-import followed by a restart gives the same final counts as a clean run.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres@127.0.0.1:5432/csvimport` | Postgres |
| `DATA_DIR` | `/data/imports` | local storage for uploaded files |
| `MAX_UPLOAD_MB` | `300` | upload size cap |
| `CHUNK_SIZE` | `10000` | rows per chunk and per transaction |
| `DEFAULT_REGION` | `IN` | country for numbers with no country code and no `country` value |
| `IMPORT_CONCURRENCY` | `4` | imports processed in parallel (one per campaign at a time). Use `1` for pinned core-cap benchmarks |
| `POOL_SIZE` | cores − 1 | validation processes |
| `LOCK_TIMEOUT` | `10 min` | when a silent worker's job is re-claimed |
| `ALLOW_RESET` | `1` | show the **reset db** button (bottom-left). It runs `TRUNCATE` on all import tables and deletes stored CSVs, so the same file can be re-tested. Set it to `0` anywhere real |

## Design docs

The justification, downsides, alternatives (Celery, SQS, S3, …) and a debate guide are in [`docs/`](docs/README.md). The diagrams are in [`docs/diagrams/`](docs/diagrams/README.md). The docs describe the fuller design, which includes a column-mapping step. The POC drops that step and requires the fixed schema above.
