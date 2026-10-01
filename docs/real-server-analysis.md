# Real server analysis: campaign CSV import worker

**Sources:**
- The CSV Import Worker documentation (3 pages).
- A 12-day CubeAPM export for the Pub/Sub consumer `sd-import-campaigns-contacts-csv-pub-sub` (Sep 19 – Oct 1): metrics, 400 sampled root spans, one full trace, and error logs.

Everything in "What the numbers say" is computed from that export. Anything I could not measure is marked *estimate* or *needs checking*.

---

## TL;DR

> **The worker spends ~42 ms per CSV row.** About 85% of that is a chain of ~6 sequential network calls made for every row: MySQL selects and single-row auto-committed inserts, a Redis write, a Pub/Sub publish, and MongoDB logs.
>
> At 42 ms per row, 10k rows take ~7 minutes and 20k rows take ~14 minutes. 14 minutes is past the 10-minute budget, so the message is NACKed and redelivered, which adds even more time.
>
> **The fix is batching, not more servers.** Process 500–1,000 rows at a time, with a handful of bulk queries and **one commit per batch**. That should bring 20k rows down to a few seconds (*estimate*: 100×+ faster). It also cuts MySQL write load by about 50×.

The single biggest cost is `contacts_mapping/insert`: **1.4M single-row inserts at ~17.5 ms each**, about 15 ms of every row. Most of that is the per-statement commit (fsync and replication on Cloud SQL), not the insert itself.

---

## 1. How the import works today (reconstructed)

From the documentation plus what the traces show:

```
cron insertCampaignsCSV (every N min)
  └─ picks ≤20 campaigns (cron_status=0, csv_url set, active) → sets cron_status=-9 → 1 Pub/Sub message per campaign

Pub/Sub consumer (NestJS, several pods)
  ├─ Redis: lock (exists/set), read resume counter  "${campaignId}_${processId}"
  ├─ MySQL: SELECT field_campaign_mapping WHERE campaign_id=?      ← ~219 ms (!)
  ├─ MySQL: UPDATE field_campaign_mapping SET cron_status … ×N      (one per mapped field)
  ├─ MySQL: SELECT contact_fields … ×N                              (one per mapped field)
  ├─ CSV: Redis cache csvdata:<url> (1h TTL, whole file as JSON) or download (filestack/S3) → array in memory
  │
  ├─ FOR EACH ROW (sequential, awaited):
  │     skip if row ≤ counter
  │     10-min budget check → NACK + break
  │     phone: primary column, else number-type custom fields; isInvalidNumber (len ≥ 5, numeric)
  │     MySQL  SELECT contacts WHERE company_id=? AND number_int IN (raw, strip1, strip2, strip3) AND status=?
  │     MySQL  INSERT contacts                     (new)        or  UPDATE contacts SET status / details (duplicate)
  │     MySQL  SELECT contacts_mapping WHERE contact_id=? AND campaign_id=?
  │     MySQL  INSERT contacts_mapping             (or UPDATE: revive DELETED→ACTIVE)
  │     Pub/Sub publish UPDATE_CSV_CUSTOM_FIELDS    (if new or overwrite), in Promise.all with the mapping write
  │     Mongo  contacts_log  (createCollection / findOne / findOneAndUpdate)   for duplicates and errors
  │     Redis  SETEX counter (30-day TTL)
  │
  └─ finalUpdate: campaigns.cron_status=1, notification API, internal error-report API,
                  COUNT contacts_mapping, campaign_metrics select/insert/update, Firebase push
```

One real trace shows the per-row loop clearly. These are 5 consecutive rows, all duplicates, each taking 16–20 ms:

```
contacts/select          1.4 ms
contacts/update          1.4 ms
contacts_mapping/select  1.3 ms
contacts_mapping/insert 10.3 ms   ← one auto-committed INSERT
Redis setex              1.0 ms
                        ─────────
                        ~16 ms per row, 5 round trips, 2 commits
```

---

## 2. What the numbers say

### 2.1 Volume

| | Value |
|---|---|
| Import messages (root transactions) | 3,225 (about 1 per campaign: the end-of-import notification calls = 3,213) |
| Rows processed | **≈1.59M** (= `Redis/setex` calls, one per row) |
| Average rows per import | ≈ 494 |
| Total time inside the consumer | 66,946 s |
| **Time per row** | **66,946 s ÷ 1.59M ≈ 42 ms** |
| Peak cluster-wide throughput | ≈ 82 rows/s (5-min average, all pods together) |
| Latency per message | p50 5.4 s · p95 58 s · p99 236 s · max sampled 398 s |

Projected from 42 ms/row:

| Rows | Today | Notes |
|---|---|---|
| 1,000 | ~42 s | |
| 10,000 | **~7 min** | |
| 20,000 | **~14 min** | Exceeds the 10-minute budget → NACK → redelivery → more time |
| 50,000 (hard limit) | ~35 min | 4+ deliveries |

Your observation of "10–20k rows in ~5 min" is 15–30 ms/row. That matches files that are mostly duplicates, which take the cheaper path (~16 ms/row, as in the trace above). Files with many new contacts are slower.

### 2.2 Per-row cost breakdown

The APM records latency for only a **sample** of calls. For example, `contacts_mapping/insert` has 1.4M calls but only 16k timed. This is why the report concluded that "most root time is unexplained".

To fix that, I multiplied each operation's average latency by its **real call count**. The result explains about **85% of the total time**, so the time is not unexplained. It's spent on per-row I/O.

| Operation | Calls | Per row | Avg | **ms per row** |
|---|---:|---:|---:|---:|
| MySQL `contacts_mapping/insert` | 1,404,820 | 0.88 | 17.5 ms | **15.4** |
| MySQL `contacts/insert` | 642,025 | 0.40 | 19.6 ms | **7.9** |
| MySQL `contacts/update` | 828,672 | 0.52 | 5.2 ms | 2.7 |
| MySQL `contacts_mapping/select` | 1,470,925 | 0.92 | 2.0 ms | 1.9 |
| MySQL `contacts/select` | 1,460,074 | 0.92 | 2.0 ms | 1.8 |
| MongoDB `contacts_log` (+ `createCollection`) | 738,614 | 0.46 | 2.6–5.8 ms | 1.6 |
| Redis `setex` (resume counter) | 1,591,929 | 1.00 | 1.1 ms | 1.1 |
| MySQL `contacts_mapping/update` | 63,450 | 0.04 | 16.6 ms | 0.7 |
| Per-message fixed costs, spread over 494 rows | — | — | ~1.2 s | ~2.4 |
| Pub/Sub publish (gRPC) | 1,336,502 | **0.84** | not exported | *(runs in parallel with the mapping write)* |
| **Accounted** | | **≈ 5.2 DB round trips + 0.84 publishes per row** | | **≈ 35 of 42 ms** |

What this shows:
- **The writes are slow because each one is its own transaction.** Selects take ~2 ms (that's the network round trip), but inserts take 17–20 ms. The extra ~15 ms is the commit: redo-log fsync, binlog, and HA replication on Cloud SQL. You pay it about **1.8 times per row**.
- **Reads are cheap but numerous.** ~1.8 selects per row, all of which could be one query per batch.
- **1.34M Pub/Sub publishes ≈ one per row.** That's one RPC per row here, plus a second per-row consumer downstream doing more single-row work.
- **`createCollection` was called 203,723 times.** That's about one call per 8 rows, when it should be zero during an import.

### 2.3 Other findings

| # | Finding | Evidence |
|---|---|---|
| A | **`field_campaign_mapping` lookup takes 177–219 ms**: a full table scan | `SELECT … FROM field_campaign_mapping WHERE campaign_id = ?` averages 219 ms, but a primary-key lookup takes ~1 ms. The `campaign_id` column almost certainly has **no index**. Costs ~0.2 s × every message. |
| B | Per-field `UPDATE field_campaign_mapping SET cron_status` and `SELECT contact_fields` run one statement each | 13,375 of each, ≈ 4 per message, ~16 ms each |
| C | Slowness isn't caused by load | Volume vs p95 correlation ≈ 0.01. Slow imports are simply **big files × 42 ms/row**. |
| D | Slowness isn't one bad pod | Slow traces appear on 9 pods across 3 ReplicaSets (3 deploys in 12 days). |
| E | Redis and Mongo are not slow individually (~1 ms and ~3 ms) | But they're called per row, so they add ~2.7 ms per row. |
| F | No errors were recorded for this consumer | `try/catch` logs and continues, so failed rows are invisible to APM (see §3, issue 8). |

---

## 3. Issues, ranked by impact

| # | Issue | Cost today | Fix | Effort |
|---|---|---|---|---|
| 1 | **Row-at-a-time writes, each auto-committed** (contacts insert/update, mapping insert) | ~26 ms/row (≈ 60%) | Batch 500–1,000 rows; bulk `INSERT … VALUES (…),(…)`, `ON DUPLICATE KEY UPDATE`; **one transaction per batch** | Medium |
| 2 | **Row-at-a-time reads for dedupe** (contacts select + mapping select) | ~3.7 ms/row | One `SELECT … WHERE number_int IN (all variants of the batch)` and one `SELECT … WHERE campaign_id=? AND contact_id IN (…)` per batch | Medium (same change as #1) |
| 3 | **Pub/Sub publish per row** for custom fields (1.34M), and a per-row consumer downstream | RPC per row + a second pipeline | Write custom fields in the same batch transaction, or publish **one message per batch** with all contact ids and values | Medium |
| 4 | **Redis counter `SETEX` per row**, separate from the MySQL write | 1.1 ms/row; not atomic with the data | Save the checkpoint **once per batch, in the same MySQL transaction** (e.g. `campaigns.csv_rows_done`) | Small |
| 5 | **Mongo logs per row, and `createCollection` 204k times** | ~1.6 ms/row | Collect logs in an array and `insertMany` once per batch; create the collection at startup only (or let it auto-create) | Small |
| 6 | **Missing index on `field_campaign_mapping.campaign_id`** | 0.2 s per message, growing with the table | `CREATE INDEX idx_fcm_campaign ON field_campaign_mapping (campaign_id);` | **Trivial** |
| 7 | **10-minute budget + NACK** turns big files into multi-delivery imports (with Pub/Sub backoff gaps in between) | Big files take ≥ 2× longer | Disappears after #1–#5 (50k rows in well under a minute). Keep the budget as a safety net. | — |
| 8 | **Silent data loss on transient errors.** The counter advances even when a row's DB write threw, so the row is never retried. Also, a crash between the INSERT and the Redis SETEX re-processes the row, which creates a real duplicate for `allowDuplicateContacts` companies. A Redis read error resets the counter to 0, which re-imports everything. | Correctness | The checkpoint lives in the same transaction as the batch. On a DB error, roll back the batch and NACK; don't skip. Only data errors (bad numbers) become log rows. | Comes with #1 and #4 |
| 9 | **Fragile duplicate matching**: `number_int IN (raw, strip 1/2/3 leading digits)` | False matches: stripping digits from `9876543210` gives `876543210`, which matches an unrelated stored 9-digit number. It's also one-directional: `9876543210` does not match a stored `919876543210`. | Store a **normalized E.164 / national number** column (libphonenumber), index `(company_id, number_norm)`, and dedupe by equality | Medium (backfill) |
| 10 | **Weak validation**: `isInvalidNumber` = length ≥ 5 and numeric | Wrong numbers become contacts and get dialed | `libphonenumber-js` `isValid()` with the campaign's country (see `csv-import-guide-bullmq.md` §4) | Small |
| 11 | **Whole CSV kept as JSON in Redis** (up to 75 MB key, 1h TTL) and as an in-memory array | Large Redis values block Redis and cost RAM on every pod; resume re-reads everything | Download to the pod's disk (or stream from GCS/S3) and skip to the checkpoint row; no Redis copy | Small–Medium |
| 12 | Each deploy (SIGTERM) interrupts running imports | Redelivery and re-processing | Graceful shutdown: stop at the next batch boundary, commit, NACK | Small |

---

## 4. Target design (same stack: NestJS + Pub/Sub + MySQL, no new infrastructure)

```
message {campaignId}
  load campaign + mapping (indexed), checkpoint = campaigns.csv_rows_done
  stream CSV from disk/GCS, skip rows ≤ checkpoint
  every 1,000 rows:
     1. validate + normalize phones in JS (libphonenumber), dedupe inside the batch with a Map
     2. SELECT id, number_norm, status FROM contacts
           WHERE company_id=? AND number_norm IN (…1,000…)                       -- 1 query
     3. INSERT INTO contacts (…) VALUES (…),(…),…                               -- new numbers, 1 query
        SELECT id, number_norm … WHERE number_norm IN (new ones)                  -- fetch the new ids, 1 query
     4. UPDATE contacts SET status=ACTIVE WHERE company_id=? AND id IN (…)      -- reactivate, 1 query
        (overwrite=1: one bulk upsert by id, or a temp-table join)
     5. INSERT INTO contacts_mapping (contact_id, campaign_id, source, status) VALUES …
           ON DUPLICATE KEY UPDATE status = IF(status = 2, 0, status), updated_at = NOW()   -- 1 query
     6. custom fields: one bulk upsert (or one Pub/Sub message per batch)        -- 1 query
     7. UPDATE campaigns SET csv_rows_done=?, counters… WHERE id=?              -- checkpoint
     COMMIT                                                                     -- 1 fsync per 1,000 rows
     Mongo insertMany(logs for this batch); await setImmediate()
  finalUpdate (unchanged)
```

| | Today (per 1,000 rows) | Target (per 1,000 rows) |
|---|---|---|
| MySQL statements | ~3,700 | **~7** |
| Commits (fsync + replication) | ~1,800 | **1** |
| Redis / Mongo / Pub/Sub calls | ~1,000 / ~460 / ~840 | 0 / 1 / 0–1 |
| Time | ~42 s | **~0.1–0.3 s** *(estimate: bulk statements of 1,000 rows on Cloud SQL)* |
| 20k-row import | ~14 min, NACK + redelivery | **~2–6 s** *(estimate)* |

Prerequisites to check before building it:
- **Does `contacts_mapping` have `UNIQUE (contact_id, campaign_id)`?** `ON DUPLICATE KEY` needs it. If it doesn't, do one bulk `SELECT contact_id, status … WHERE campaign_id=? AND contact_id IN (…)`, then bulk-insert the missing rows and bulk-update the deleted ones.
- **`allowDuplicateContacts` companies:** skip step 2 and bulk-insert everything. You can't add a unique index on `(company_id, number)` because of these companies.
- **Two imports for the same company at the same time** can both insert the same new number between steps 2 and 3. Today's code has the same race. Either serialize imports per company (a per-company lock key, like your current Redis lock), or use `SELECT … FOR UPDATE` on the batch's numbers.
- Batch size: around 1,000 rows. With 4 variants per number that's ~4,000 values in one `IN (…)`, well within MySQL limits. Keep each transaction under ~1 s.

### Sketch (TypeORM / mysql2 raw SQL inside one transaction)

```ts
async function flushBatch(qr: QueryRunner, ctx: Ctx, batch: Row[]) {
  await qr.startTransaction();
  try {
    const byNum = new Map<string, Row>();
    for (const r of batch) if (!byNum.has(r.numberNorm)) byNum.set(r.numberNorm, r);  // in-file dedupe
    const nums = [...byNum.keys()];

    const existing: { id: number; number_norm: string; status: number }[] = nums.length ? await qr.query(
      `SELECT id, number_norm, status FROM contacts WHERE company_id = ? AND number_norm IN (?)`,
      [ctx.companyId, nums]) : [];
    const idByNum = new Map(existing.map(c => [c.number_norm, c.id]));

    const fresh = nums.filter(n => !idByNum.has(n));
    if (fresh.length) {
      await qr.query(`INSERT INTO contacts (company_id, user_id, number_int, number_norm, name, email, status) VALUES ?`,
        [fresh.map(n => { const r = byNum.get(n)!; return [ctx.companyId, ctx.userId, r.numberInt, n, r.name, r.email, ACTIVE]; })]);
      for (const c of await qr.query(`SELECT id, number_norm FROM contacts WHERE company_id = ? AND number_norm IN (?)`,
        [ctx.companyId, fresh])) idByNum.set(c.number_norm, c.id);
    }
    const revive = existing.filter(c => c.status !== ACTIVE).map(c => c.id);
    if (revive.length) await qr.query(`UPDATE contacts SET status = ? WHERE company_id = ? AND id IN (?)`, [ACTIVE, ctx.companyId, revive]);

    await qr.query(
      `INSERT INTO contacts_mapping (contact_id, campaign_id, source, status) VALUES ?
       ON DUPLICATE KEY UPDATE status = IF(status = 2, 0, status), updated_at = NOW()`,
      [nums.map(n => [idByNum.get(n), ctx.campaignId, ctx.source, 0])]);

    await qr.query(`UPDATE campaigns SET csv_rows_done = ? WHERE id = ?`, [batch.at(-1)!.rowNo, ctx.campaignId]);
    await qr.commitTransaction();
  } catch (e) { await qr.rollbackTransaction(); throw e; }   // → NACK; the retry resumes at csv_rows_done
}
```

(`VALUES ?` with an array of arrays and `IN (?)` with an array are mysql2's `query()` expansion. Use `query`, not `execute`, for bulk statements.)

---

## 5. Order of work

| Step | Change | Expected effect | Risk |
|---|---|---|---|
| 1 | `CREATE INDEX … field_campaign_mapping(campaign_id)` | −0.2 s per message, less DB CPU | None (online DDL) |
| 2 | Remove the per-row `createCollection`; buffer Mongo logs and `insertMany` per batch | −1.6 ms/row | Low |
| 3 | Batch the Pub/Sub custom-field publishes (or inline them) | −1 RPC/row here and in the downstream consumer | Low–Medium |
| 4 | **Batched MySQL writes + one transaction + MySQL checkpoint** (§4) | **42 ms/row → ~0.1–0.3 ms/row** *(estimate)*, and fixes the silent row loss | Medium: test on a staging copy, compare counts with the old path |
| 5 | Normalized number column + libphonenumber validation | Correct dedupe, fewer wrong numbers | Medium: needs a backfill |
| 6 | CSV from disk/GCS instead of Redis JSON; graceful shutdown | Less Redis and pod memory; clean deploys | Low |

---

## 6. How to verify each step in CubeAPM

| Metric | Today | After step 4 |
|---|---|---|
| `contacts_mapping/insert` calls ÷ `Redis/setex` (or rows) | 0.88 per row | ≈ 0.001 per row |
| Root `latency_sum` ÷ rows processed | ~42 ms/row | < 1 ms/row |
| Root p95 / p99 | 58 s / 236 s | a few seconds |
| Messages per campaign (NACK redeliveries) | > 1 for big files | 1 |
| Cloud SQL write IOPS and commits/s during imports | high | ~1/1000 of today |

Two instrumentation fixes would make the next export conclusive:
- Add a custom attribute `rows_processed` on the root transaction.
- Raise the New Relic `max_trace_segments` for this consumer, or add one custom segment per batch. Today, long imports exceed the segment cap, so their children aren't captured: the 398 s trace has ~389 s of "exclusive" root time.

## 7. Open questions (need access I don't have)

- Cloud SQL tier, HA on/off, and `innodb_flush_log_at_trx_commit` / `sync_binlog`. These explain why a single insert costs 10–20 ms.
- `SHOW CREATE TABLE` for `contacts`, `contacts_mapping` and `field_campaign_mapping` to confirm indexes and the unique keys required in §4.
- The Pub/Sub subscription settings (ack deadline, `maxMessages` flow control, retry backoff) and how many imports run concurrently per pod.
- What the downstream `UPDATE_CSV_CUSTOM_FIELDS` consumer does per message. It probably has the same per-row pattern.
