# 2. Downsides and risks of the chosen approach

This section is deliberately blunt. Every item has a severity for *our* scale (up to 1M rows, a handful of imports per hour, one server) and a mitigation. Items are ordered by how likely they are to hurt us first.

| # | Downside | Severity now | Gets worse when… |
|---|---|---|---|
| 0 | Numbers without a country code are ambiguous | **High** | Files mix countries and have no country column |
| 1 | Single box = single point of failure | High | We promise uptime or add nodes |
| 2 | Uploads go through our API and can't resume | Medium–High | Users have slow or flaky connections (mobile, office VPN) |
| 3 | Head-of-line blocking with 1 import slot | Medium | Many users import at the same time |
| 4 | Local disk ties api and worker to one machine | Medium | We want a second app node |
| 5 | "First row wins" dedupe loses better data | Medium | Later files carry corrected names or vars |
| 6 | Rejected-rows CSV has only `raw_phone`, not the full row | Medium | Users actually try to "fix and re-upload" |
| 7 | Encoding assumptions (UTF-8) | Medium | Excel "Unicode text" (UTF-16) or cp1252 exports |
| 8 | CSV/formula injection in exports | Medium (security) | Someone opens the export in Excel |
| 9 | Hand-rolled queue code | Low–Medium | The person who wrote it leaves |
| 10 | Resume re-parses from row 1 | Low | 1M-row files crash near the end |
| 11 | Unique-index merge is the throughput ceiling | Low | `contacts` reaches hundreds of millions of rows |
| 12 | Preview samples only the first 50 rows | Low | Files sorted so the top rows aren't representative |
| 13 | Landline detection depends on libphonenumber metadata | Low | "Reject landlines" is switched on |
| 14 | Polling-based progress | Very low | Hundreds of people watching imports at once |

---

### 0. Numbers without a country code are ambiguous

- **What:** a local UK number (`07775 559513`) and a local US number (`(717) 751-1028`) are *also* valid Indian mobiles.
  - If the file has no country column, they're read in the default country.
  - The result is a **valid but wrong** number. It won't show up as a rejection; the campaign dials a stranger.
  - Measured: 2,955 of about 10,000 foreign rows in a mixed test file.
- **Mitigation now:**
  - Map a country column (auto-suggested when the header says "country").
  - Set the default country to the list's main country.
  - Numbers written with `+` or `00` are never ambiguous.
- **Mitigation next:**
  - Warn in the mapping step when no country column is mapped and the sample contains numbers valid in more than one country.
  - Record in each contact which rule decided its country, so suspicious ones can be reviewed before dialling.

### 1. Single box = single point of failure

- **What:** api, worker, Postgres and the uploaded files all live on one host. If the host dies, imports stop *and* the API is down.
- **Why we accept it:** the rest of the product already runs this way. The import pipeline adds no new failure mode.
- **Mitigation:**
  - Run Postgres with WAL archiving or managed backups.
  - Keep `imports.status` recoverable. After a restart, stale locks are re-claimed automatically (tested with `kill -9`).
  - Losing the raw CSV means asking the user to re-upload. Nothing gets corrupted.
- **Exit:** managed Postgres, plus object storage for files ([A4](alternatives/04-object-storage-serverless.md)). The worker code barely changes.

### 2. Uploads go through our API and can't resume

- **What:** a 250 MB upload over a 5 Mbps link takes about 7 minutes. Any network blip means starting over.
- **Other costs:**
  - The upload occupies an API connection for the whole transfer.
  - Reverse proxies need `client_max_body_size ≥ 300M` and long read timeouts.
  - Hashing runs on the event loop. It's cheap, but it isn't zero.
- **Mitigation now:** set proxy limits explicitly and show clear progress in the UI (done). Idempotent re-upload means retrying is safe.
- **Exit:** presigned multipart upload straight to S3/GCS/MinIO, which is resumable per part and never touches our API ([A4](alternatives/04-object-storage-serverless.md)). Alternatively, the tus protocol against our own server.

### 3. Head-of-line blocking with one import slot

- **What:** `IMPORT_CONCURRENCY=1` means a 5k-row file waits behind a 1M-row file, about 40 s. At 2 slots, two big files still block everyone else.
- **Mitigation:**
  - Order the claim by `(est_rows > 200k, created_at)` so small files jump the queue. This is a one-line change to `ORDER BY`.
  - Raise the limit to 2 on boxes with 8 or more cores.
  - Longer term, run a separate "small files" slot.

### 4. Local disk ties api and worker to one machine

- **What:** the worker opens `/data/imports/{id}.csv`, so a worker on another host can't see it.
- **Mitigation:** a shared volume (NFS/EFS) works but is slow and fragile. The real fix is object storage: the worker streams from S3 with a ranged GET, and the CSV reader stays the same.

### 5. "First row wins" dedupe loses better data

- **What:** `ON CONFLICT DO NOTHING` keeps whichever row arrived first, whether earlier in the file or in an earlier file. If a later file has a corrected name or new vars, those are silently dropped. They're counted as `duplicate_rows`, but the data isn't merged.
- **Mitigation:** decide the product rule explicitly:
  - `DO UPDATE SET name = EXCLUDED.name, vars = contacts.vars || EXCLUDED.vars WHERE contacts.status = 'pending'`, which means last write wins for contacts not yet called.
  - Updates are slower than `DO NOTHING` (more WAL, dead tuples), roughly 1.5–2× on the merge step (*estimate*).

### 6. The rejected-rows CSV isn't "fix and re-upload" ready

- **What:** `import_errors` stores `row_no, raw_phone, reason`. The user gets row numbers, not the original rows, so fixing means cross-referencing with their original file.
- **Mitigation options:**
  1. Store the raw line's byte offset and re-read those lines from the original file during export. This only works while the file is inside the retention window.
  2. Store the mapped columns (name, vars) in `import_errors` as well. This makes the table bigger, but it's self-contained.
- **Recommendation:** option 2. It's cheap and survives retention.

### 7. Encoding assumptions

- **What:** we decode UTF-8 (BOM-tolerant) with `errors="replace"`. That causes two problems:
  - Excel's "Unicode text" export is UTF-16, which would parse as garbage and yield zero valid rows.
  - cp1252 files produce `�` in names.
- **Mitigation:** sniff the encoding in the preview: check for a UTF-16 BOM or NUL bytes, and try a strict UTF-8 decode of the first 64 KB. Store it in `imports.encoding`, and warn in the mapping UI when the names look broken. This is about 20 lines of code.

### 8. CSV/formula injection in exports

- **What:** `vars` and `name` come from user files. A cell like `=HYPERLINK(...)` exported via `COPY TO STDOUT` and opened in Excel executes as a formula.
- **Mitigation:** prefix values that start with `= + - @ \t \r` with `'` in the export query, or sanitise at ingest. This should be done before the feature is exposed to customers.

### 9. Hand-rolled queue code

- **What:** claim, heartbeat, stale-lock recovery, the concurrency cap and graceful release come to about 120 lines we own. They're easy to get subtly wrong: lock-timeout units, clock skew, a forgotten `worker_id` guard.
- **Mitigation:** the ownership guard plus the `kill -9` test cover the critical path. Add that crash test to CI.
- **Exit:** a Postgres queue library gives the same semantics with less custom code ([A3](alternatives/03-postgres-queue-library.md)).

### 10. Resume re-parses from row 1

- **What:** after a crash at row 900k, the new worker parses 900k rows just to skip them, which takes about 10 s. That's harmless, but wasteful.
- **Mitigation:** store the byte offset of the chunk boundary alongside `checkpoint_row` and `seek()` there. This is safe because chunk boundaries are always at record boundaries.

### 11. The unique-index merge is the ceiling

- **What:** about 60% of chunk time is `INSERT … ON CONFLICT` maintaining `UNIQUE (campaign_id, phone_e164)`. As `contacts` grows into the hundreds of millions, index pages fall out of cache and the merge slows down.
- **Mitigation:**
  - Partition `contacts` by `campaign_id` (hash or list), so each campaign's index stays small.
  - Archive finished campaigns.
  - Run `ANALYZE` after big imports (done).

### 12. Preview samples only the first 50 rows

- **What:** if a file is sorted so the first 50 rows have an empty `Mobile Phone`, the suggestion is wrong. The human confirmation catches this, but only if the user reads it.
- **Mitigation:** reservoir-sample about 500 rows by reading the first ~2 MB plus a few random byte offsets, re-synced to line starts.

### 13. Landline detection depends on metadata

- **What:** Indian STD codes like 080 or 079 overlap the mobile fast path. We handle that, but libphonenumber classifies some number ranges ambiguously. On random synthetic numbers, about 14% were flagged `landline`; real data should flag far fewer.
- **Mitigation:** keep "reject landlines" **off by default** (it is), and show the landline count before anyone relies on it.

### 14. Polling-based progress

- **What:** each watcher sends one small indexed query per second. At 100 concurrent watchers that's 100 QPS, which is still trivial.
- **Exit:** SSE fed by `LISTEN/NOTIFY` from the chunk transaction, if we ever need it.

---

## Things that look like risks but aren't

- **"`synchronous_commit = off` loses data."** A crash loses the last few chunk commits *and* their checkpoints together, and the worker redoes those chunks. No partial state is possible.
- **"Temp tables leak."** They're per connection, `ON COMMIT DELETE ROWS`, and dropped when the connection closes.
- **"Postgres as a queue doesn't scale."** It doesn't scale to 10k msgs/s of tiny jobs with naive polling. We have a few jobs per hour, each minutes long. Queue overhead is about 1 query/s per idle worker.
- **"COPY bypasses constraints."** It doesn't. COPY into `stage` has no constraints by design, and the merge into `contacts` enforces all of them.
