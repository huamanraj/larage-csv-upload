# Contact import microservice: design (same behaviour, new architecture)

**Goal:** move the campaign CSV import out of `sub2-salesdialer-nest` into its own service, `sd-contact-import`. It must behave exactly like the current worker, but take **seconds instead of minutes** and never lose or double-import a row.

**Inputs:**
- The current worker's behaviour, from the CSV Import Worker documentation.
- The production numbers from `real-server-analysis.md`.
- The batch design measured in this repo's POC.

Table and column names of the existing schema come from the production traces. Names marked *(check)* weren't visible there.

---

## 1. What stays the same (functional parity checklist)

Every row below must behave identically after the move. Use this as the acceptance test list.

| # | Current behaviour | In the new service |
|---|---|---|
| 1 | A campaign with `cron_status = 0`, a `csv_url` and active status gets imported | Same rule. The cron becomes a backup sweeper; normally the import starts as soon as the CSV is attached (§3) |
| 2 | `cron_status`: 0 → -9 (in progress) → 1 (ran) | Same values, written by the service |
| 3 | Limits: 50,000 rows, 25 MB (75 MB for customized companies) | Same limits, read from config per company |
| 4 | Columns are mapped by position (`field_campaign_mapping.field_index`) | Same. The mapping is read once per import and saved on the job |
| 5 | The phone comes from the main column, else from number-type custom fields (`insertAlternative`) | Same priority order: the first valid one wins |
| 6 | An invalid number skips the row and writes an error-report entry | Same, with a stored reason (`empty`, `too_short`, `invalid`, …) |
| 7 | Invalid email → blanked; birthday cut to 10 characters; bad deal size → "don't update" | Same rules |
| 8 | `allowDuplicateContacts` companies: no dedupe, every row is a new contact | Same (`dedupe_mode = none`) |
| 9 | `checkInsertContactMethod` companies: exact match on number | Same (`dedupe_mode = exact`) |
| 10 | Everyone else: "variation" match (number with 1–3 leading digits stripped) | `dedupe_mode = variation`. Same variants, but computed for the whole batch in one query (§5). Later replaced by normalized-number matching (§7) |
| 11 | Duplicate + `overwrite = 1` → update the contact's details; otherwise reactivate it | Same, as one bulk update per batch |
| 12 | Duplicate → refresh the mapping's `updated_at`; DELETED (2) mapping → ACTIVE (0) | Same, as one bulk upsert per batch |
| 13 | Custom fields saved only if new contact, or duplicate with overwrite | Same rule, saved in the batch transaction (no per-row Pub/Sub message) |
| 14 | Error report in MongoDB `contacts_log` | Same collection and fields, written with `insertMany` once per batch |
| 15 | At the end: campaign → RAN, AD notification, error-report API, contact recount, Firebase push | Same steps, triggered by an `import.completed` event (§3) |
| 16 | Resumable after a stop | Resumable from the last committed batch (exact, never repeats or skips a row) |

---

## 2. What changes and why

| Today | New | Why |
|---|---|---|
| One row at a time, ~6 network calls per row, each auto-committed | 1,000-row batches, ~8 queries and **1 commit per batch** | Removes ~85% of the time (42 ms → under 1 ms per row) |
| Resume counter in Redis, separate from the data | Checkpoint stored on the job row, **in the same transaction** as the batch | A crash can't lose or repeat rows; a Redis error can't restart the import from 0 |
| A row whose DB write throws is skipped silently | The batch rolls back and is retried; only bad *data* becomes an error row | No silent data loss |
| A Pub/Sub message per row for custom fields | Custom fields written in the same batch | Removes 1.3M messages per 12 days, and the downstream per-row consumer |
| Mongo log calls per row (plus `createCollection` 204k times) | One `insertMany` per batch | |
| Whole CSV cached in Redis as JSON (up to 75 MB) | Streamed from GCS; nothing large in Redis | Redis and pod memory stay small |
| 10-minute budget, NACK and redelivery | Not needed: 50k rows take seconds. Kept as a lease timeout for crashed workers | |
| Waits for the cron (≤20 campaigns per run) | Starts immediately; the cron only sweeps up missed ones | No queue delay |
| No limit per company | One running import per company; different companies run in parallel | Two imports for the same company can't create the same contact twice |
| Missing index on `field_campaign_mapping.campaign_id` | Index added | −0.2 s per import |

---

## 3. Architecture

```
main app (sub2-salesdialer-nest)                     sd-contact-import
────────────────────────────────                     ─────────────────────────────────────────────
user uploads CSV ──► GCS (signed URL upload)
campaign saved (csv_url, cron_status=0)
  └─ POST /imports {campaign_id} ──────────────────► import-api
     (or publishes contact-import.requested)          ├─ INSERT contact_imports (status=queued)
                                                      └─ publish contact-import.requested {import_id}
                                                                       │
                                                      import-worker (N pods, 2–4 imports each)
                                                      ├─ claim job (lease) + per-company lock
                                                      ├─ stream CSV from GCS, skip ≤ checkpoint
                                                      ├─ every 1,000 rows: validate → 1 MySQL transaction
                                                      │     (contacts, contacts_mapping, custom fields,
                                                      │      import errors, checkpoint)
                                                      │   → Mongo insertMany (error report)
                                                      └─ done → publish contact-import.completed
                                                                       │
main app listens to contact-import.completed ◄─────────────────────────┘
  ├─ campaigns.cron_status = 1, recount contacts, campaign_metrics
  ├─ AD notification, error-report API
  └─ Firebase push

cron (every 5 min): campaigns with cron_status=0 and no job → create job (safety net)
```

**Two deployments, one codebase:**
- **`sd-contact-import-api`:** small. It creates jobs, returns progress, and lists errors.
- **`sd-contact-import-worker`:** does the work. It scales by replica count and has its own CPU and memory limits, so imports can never starve the dialer pods.

**Boundary rule:**
- The service is the only writer of contacts **during an import**.
- The main app still owns the `contacts` / `contacts_mapping` schema and its migrations.
- The service reaches those tables only through one module (`ContactWriter`), so any schema change touches a single place.
- Side effects that belong to the main app (notifications, Firebase, metrics) stay there, triggered by the completion event.

**Queue:** Pub/Sub, because you already run it. Only the `import_id` travels in the message. The job row in MySQL is the source of truth, so a lost or duplicated message is harmless: the claim step ignores jobs already running or done.

---

## 4. Tables

### New tables (owned by the service)

```sql
-- One row per import. It is the job, the progress and the resume point.
CREATE TABLE contact_imports (
  id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  company_id       BIGINT UNSIGNED NOT NULL,
  campaign_id      BIGINT UNSIGNED NOT NULL,
  user_id          BIGINT UNSIGNED NOT NULL,
  source_url       VARCHAR(1024)   NOT NULL,           -- gs://… or the existing csv_url
  file_sha256      CHAR(64)        NULL,               -- same file twice into a campaign = same import
  file_bytes       BIGINT UNSIGNED NULL,
  status           ENUM('queued','processing','done','failed','cancelled') NOT NULL DEFAULT 'queued',
  -- behaviour flags, frozen at creation so a config change mid-import can't change the result
  dedupe_mode      ENUM('variation','exact','none') NOT NULL,
  overwrite        TINYINT(1)      NOT NULL DEFAULT 0,
  insert_alternative TINYINT(1)    NOT NULL DEFAULT 0,
  row_limit        INT UNSIGNED    NOT NULL DEFAULT 50000,
  mapping          JSON            NOT NULL,           -- column index -> field, read once from field_campaign_mapping
  -- progress (the UI reads these; never COUNT(*) over contacts)
  rows_total       INT UNSIGNED    NULL,
  checkpoint_row   INT UNSIGNED    NOT NULL DEFAULT 0, -- last row committed; resume skips ≤ this
  inserted_rows    INT UNSIGNED    NOT NULL DEFAULT 0, -- new contacts
  updated_rows     INT UNSIGNED    NOT NULL DEFAULT 0, -- duplicates updated (overwrite) or reactivated
  revived_mappings INT UNSIGNED    NOT NULL DEFAULT 0, -- DELETED -> ACTIVE in this campaign
  invalid_rows     INT UNSIGNED    NOT NULL DEFAULT 0,
  -- lease: a worker that dies stops renewing it, and another worker resumes the job
  worker_id        VARCHAR(128)    NULL,
  lease_until      DATETIME(3)     NULL,
  attempts         SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  error            TEXT            NULL,
  created_at       DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  started_at       DATETIME(3)     NULL,
  finished_at      DATETIME(3)     NULL,
  UNIQUE KEY uq_campaign_file (campaign_id, file_sha256),
  KEY ix_queue (status, created_at),
  KEY ix_company_status (company_id, status)          -- one running import per company
);

-- Rows that could not be imported, with the reason. Feeds the error report alongside Mongo contacts_log.
CREATE TABLE contact_import_errors (
  import_id   BIGINT UNSIGNED NOT NULL,
  row_no      INT UNSIGNED    NOT NULL,
  raw_value   VARCHAR(255)    NULL,
  reason      VARCHAR(32)     NOT NULL,              -- empty | too_short | invalid | sci_notation | junk | no_contact_id
  PRIMARY KEY (import_id, row_no)
);
```

### Changes to existing tables (owned by the main app; run as its migrations)

```sql
-- 1. Missing index: the mapping lookup takes ~219 ms today.
CREATE INDEX ix_fcm_campaign ON field_campaign_mapping (campaign_id);

-- 2. One mapping per contact per campaign. Needed so the bulk upsert can revive/refresh in one statement.
--    Clean up existing duplicate pairs first (keep the oldest).
ALTER TABLE contacts_mapping ADD UNIQUE KEY uq_contact_campaign (contact_id, campaign_id);

-- 3. Dedupe lookups for a whole batch. Check that it exists; add it if not.
CREATE INDEX ix_contacts_company_number ON contacts (company_id, number_int, status);

-- 4. One value per contact per custom field, so a batch of custom fields is one upsert.  (check table name)
ALTER TABLE contact_field_values ADD UNIQUE KEY uq_contact_field (contact_id, field_id);
```

### Later (§7): normalized numbers

```sql
ALTER TABLE contacts
  ADD COLUMN number_e164 VARCHAR(16) NULL,   -- +919876543210, set by libphonenumber
  ADD COLUMN dedupe_key  VARCHAR(16) NULL,   -- = number_e164, or NULL for allowDuplicateContacts companies
  ADD UNIQUE KEY uq_company_dedupe (company_id, dedupe_key);
-- MySQL allows many NULLs in a unique key, so "no dedupe" companies keep inserting duplicates
-- while everyone else gets dedupe enforced by the database itself, not by application code.
```

Duplicates that already exist in the data must be resolved before this unique key goes on. Set `dedupe_key` only on the oldest contact of each company + number.

---

## 5. One batch (1,000 rows), step by step

```
1. Validate in memory: phone (+ alternative columns), email, birthday, deal size → good rows, error rows
2. Dedupe inside the batch (Map by number); `none` mode skips this
3. SELECT id, number_int, status FROM contacts
     WHERE company_id = ? AND status = ? AND number_int IN (all variants of all numbers)   -- 1 query
   variation mode: variants = raw, strip 1, strip 2, strip 3 (today's rule), matched back in memory
4. INSERT INTO contacts (...) VALUES (...), (...), ...      -- new numbers, 1 query
   SELECT id, number_int ... WHERE number_int IN (new ones)  -- their ids, 1 query
5. Duplicates: UPDATE contacts SET status = ACTIVE WHERE id IN (...)         -- reactivate, 1 query
              or bulk upsert of details when overwrite = 1                      -- 1 query
6. INSERT INTO contacts_mapping (contact_id, campaign_id, source, status) VALUES ...
     ON DUPLICATE KEY UPDATE status = IF(status = 2, 0, status), updated_at = NOW(3)  -- 1 query
7. INSERT INTO contact_field_values (...) VALUES ... ON DUPLICATE KEY UPDATE value = VALUES(value)
     -- only new contacts, or duplicates with overwrite                       -- 1 query
8. INSERT INTO contact_import_errors (...) VALUES ...                        -- 1 query
9. UPDATE contact_imports SET checkpoint_row = ?, counters…, lease_until = NOW(3) + INTERVAL 2 MINUTE
     WHERE id = ? AND worker_id = ?                                          -- checkpoint + lease
   COMMIT                                                                     -- the only commit
10. Mongo contacts_log.insertMany(batch logs); yield to the event loop
```

**Per 1,000 rows:** about 8 statements and 1 commit, against ~3,700 statements and ~1,800 commits today. The ID re-select in step 4 avoids relying on MySQL's consecutive auto-increment IDs. Two imports for the same company can't race between steps 3 and 4, because only one import per company runs at a time (the claim checks `ix_company_status`).

**Claiming a job:**
```sql
UPDATE contact_imports SET status='processing', worker_id=?, lease_until=NOW(3)+INTERVAL 2 MINUTE,
       attempts=attempts+1, started_at=COALESCE(started_at, NOW(3))
WHERE id = ? AND (status='queued' OR (status='processing' AND lease_until < NOW(3)))
  AND NOT EXISTS (SELECT 1 FROM (SELECT company_id FROM contact_imports
                   WHERE status='processing' AND lease_until >= NOW(3)) r WHERE r.company_id = ?);
-- 1 row updated = this worker owns the job; 0 = someone else has it or the company is busy (nack, retry later)
```

---

## 6. Expected performance

| | Today (production, measured) | New service (estimate) |
|---|---|---|
| Time per row | ~42 ms | ~0.1–0.3 ms |
| 20k-row import | ~14 min (NACK + redelivery) | ~2–6 s |
| 50k-row import (limit) | ~35 min, 4+ deliveries | ~5–15 s |
| MySQL statements / commits per 1,000 rows | ~3,700 / ~1,800 | ~8 / 1 |
| Pub/Sub messages per import | ~1 per row | 2 (requested + completed) |
| Redis | counter per row + whole CSV cached | none needed |

The estimates come from the batch design, which this repo's POC measured on Postgres at about 1M rows in 30 s on 4 cores. Confirm them on staging against a copy of production MySQL before committing to numbers.

---

## 7. Rollout

1. **Quick wins, still in the old worker:** add the `field_campaign_mapping` index; remove the per-row `createCollection`.
2. **Build the service** with batch writes and parity mode (`variation` dedupe exactly as today).
3. **Shadow test.** Import the same CSVs into a staging copy with both old and new code. Compare per campaign: contacts created, reactivated, mappings revived, custom fields and error rows. They must match.
4. **Switch per company** behind a flag, starting with internal and small accounts. The old consumer stays as a fallback.
5. **Switch everyone; delete the old consumer**, the Redis counter and the CSV-in-Redis cache.
6. **Then improve matching:** backfill `number_e164`, add `dedupe_key`, validate with libphonenumber, and retire the strip-digits rule. This is a deliberate behaviour change and needs product sign-off, because it changes which rows count as duplicates.

---

## 8. Naming

| Thing | Name |
|---|---|
| Service / repo | `sd-contact-import` |
| Deployments | `sd-contact-import-api`, `sd-contact-import-worker` |
| Pub/Sub topics | `sd.contact-import.requested`, `sd.contact-import.completed` |
| Subscriptions | `sd-contact-import-worker.requested`, `salesdialer-nest.contact-import-completed` |
| Tables | `contact_imports`, `contact_import_errors` |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| The service and the main app both depend on the `contacts` schema | All writes go through one `ContactWriter` module; schema changes are reviewed by both owners |
| Results differ from the old worker | Parity mode plus the shadow comparison (step 3) before any switch |
| Adding the unique keys fails on existing duplicate data | Run duplicate-cleanup scripts first, measure on a production copy, use online DDL |
| A big batch transaction holds locks longer | 1,000 rows ≈ 0.1–0.3 s per commit; lower the batch size if lock waits appear |
| Completion event lost, so the campaign isn't marked RAN | The job row is the truth; the cron sweeper re-publishes `completed` for done jobs whose campaign isn't RAN |
