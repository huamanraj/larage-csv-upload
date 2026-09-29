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
| anything else | no | kept in `contacts.vars` (jsonb), non-empty values only |

**Outlook contacts export** (the standard ~92-column file)

- **Phone:** the first valid number among `Mobile Phone → Number → Primary Phone → Business Phone → Home Phone → …` wins. Fax, pager and ID-number columns are never used.
- **Name:** `First Name + Middle Name + Last Name`.
- **Country:** `Home Country/Region`, else `Business Country/Region`.
- **Everything else** (company, e-mail, address, …) goes into `vars`.

Header names are case-insensitive. A file with no usable phone column is rejected with a 422 error. A row whose numbers are all invalid ends up in `import_errors` with its reason. For example, Outlook's placeholder `555-555-1212` is not a real number, so it's rejected.

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
   - **Validate** across a process pool (cores − 1). Indian mobiles take a regex fast path; every other number goes through `phonenumbers`. The results are `+E.164`, or a rejection reason: `empty`, `invalid` or `sci_notation`.
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

Results can be paged with `GET /api/campaigns/{id}/contacts?after_id=` (keyset pagination, no `OFFSET`).

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
| `IMPORT_CONCURRENCY` | `1` | global limit on running imports |
| `POOL_SIZE` | cores − 1 | validation processes |
| `LOCK_TIMEOUT` | `10 min` | when a silent worker's job is re-claimed |
| `ALLOW_RESET` | `1` | show the **reset db** button (bottom-left). It runs `TRUNCATE` on all import tables and deletes stored CSVs, so the same file can be re-tested. Set it to `0` anywhere real |

## Design docs

The justification, downsides, alternatives (Celery, SQS, S3, …) and a debate guide are in [`docs/`](docs/README.md). The diagrams are in [`docs/diagrams/`](docs/diagrams/README.md). The docs describe the fuller design, which includes a column-mapping step. The POC drops that step and requires the fixed schema above.
