# Fast CSV contact import with BullMQ + Postgres: mapping, validation, duplicates, and why 20k rows takes 5 minutes

> **Short answer:** 10k–20k rows should take **about 1 second** to process on a single server, not 5 minutes. When an import is 100–300× slower than that, the cause is almost always **doing the work one row at a time**: one DB query, commit, BullMQ job, progress update or API call per row. Batch everything per 5k rows and the problem goes away.

---

## 1. What a 20k-row import should cost (measured)

20,000 rows, Postgres on the same machine, timed from the application:

| How rows are written | Round trips | Time |
|---|---|---|
| Per row: `SELECT` to check for a duplicate, then `INSERT`, each auto-committed | 40,000 | **11.0 s** |
| Per row: `INSERT … ON CONFLICT DO NOTHING`, auto-committed | 20,000 | **7.5 s** |
| **Batched:** 4 × `INSERT … SELECT FROM unnest(…) ON CONFLICT DO NOTHING`, one transaction | 4 | **0.23 s** |

Same test from Node with `pg` (temp table, no WAL): **3.07 s per row vs 0.105 s batched**, which is 29× faster. That's with a ~0.05 ms round trip. Real setups add more:

| Extra cost per row | Typical |
|---|---|
| Docker network / separate DB host round trip | 0.2–1 ms |
| ORM overhead (`prisma.create`, `repository.save`, `Model.create`) | 1–5 ms |
| A commit + fsync per row on a cloud disk | 1–10 ms |
| One BullMQ job per row (add, fetch, lock, complete, events: several Redis calls) | 1–5 ms |
| `job.updateProgress()` / websocket emit / `console.log` per row | 0.1–2 ms |
| An external lookup API per row ("is this number real?") | **100–500 ms** |

About **15 ms per row × 20,000 rows = 300 s = 5 minutes.** That's your number. Batched, the same import does a few dozen database calls in total.

---

## 2. Where you're probably going wrong (check in this order)

| # | Symptom in your code | Why it's slow | Fix |
|---|---|---|---|
| 1 | `for (const row of rows) { await db.query(...) }` or ORM `create()` in a loop | One round trip and one commit per row | One `INSERT … SELECT FROM unnest(arrays)` per 5k rows (§5) |
| 2 | `SELECT … WHERE phone = $1` before each insert to find duplicates | 2× the round trips, and a sequential scan per row if there's no index (gets worse as the table grows) | `UNIQUE (campaign_id, phone)` + `ON CONFLICT DO NOTHING` (§5) |
| 3 | `queue.add()` once per row, or once per small group | BullMQ costs several Redis round trips per job, plus events, plus completed-job storage | **One job per file** (or per 50k-row range); the job loops over batches |
| 4 | Calling a phone-lookup API (Twilio Lookup, NumVerify, HLR…) during the import | 100–500 ms per row, plus rate limits | Validate format offline (§4); verify reachability later, only for numbers you'll dial, in bulk, cached |
| 5 | `job.updateProgress()`, socket emit or a log line per row | Redis or I/O call per row | Update once per batch |
| 6 | Whole file read into memory, or file content put in `job.data` | Large Redis payloads and memory spikes; Redis is not a file store | Stream the upload to disk or S3, and put only `{ importId }` in the job |
| 7 | A heavy synchronous loop in the worker (parsing or validating 20k rows without yielding) | BullMQ can't renew the job lock → the job is marked **stalled** and **runs again**, sometimes several times | Process in batches and yield between them (`await setImmediate`), set `lockDuration`, or use a sandboxed processor |
| 8 | `csv-parse` with `columns: true`, or building an object per row for 90+ columns | Allocates a big object per row | Parse to arrays and pick mapped columns by index (§3) |
| 9 | Missing or extra indexes, triggers, or FK checks on `contacts` | Each one runs per inserted row | One unique index for dedupe and the indexes your queries need; nothing else |
| 10 | Import worker in the same process as your API and other heavy jobs | Competes for one Node event loop and CPU | Separate worker process, an import queue with `concurrency: 1–2` |

### Find the culprit in 10 minutes

```sql
-- Postgres: which statements run most? (CREATE EXTENSION pg_stat_statements; also add it to shared_preload_libraries)
SELECT calls, round(total_exec_time) AS ms, left(query, 80) FROM pg_stat_statements ORDER BY calls DESC LIMIT 10;
-- If 'calls' ≈ number of CSV rows, you are writing per row (problem #1 or #2).

EXPLAIN ANALYZE SELECT 1 FROM contacts WHERE campaign_id = 1 AND phone = '+919876543210';
-- 'Seq Scan' here = missing index (problem #2).
```

```bash
redis-cli --scan --pattern 'bull:*:id'   # job id counters: jobs created per queue
redis-cli ZCARD bull:<queue>:completed   # thousands per import = one job per row (problem #3)
```

```ts
worker.on('stalled', (id) => console.warn('stalled, will re-run', id)); // problem #7
console.time('parse'); /* … */ console.timeEnd('parse');             // time each stage per batch
```

---

## 3. Column mapping: the best way for CSVs with many different columns

**Goal:** guess the right columns automatically, let the user confirm once, and remember the answer for files with the same layout.

### 3.1 Read only the header and a sample

Stream the first ~200 rows (`csv-parse` with `to_line: 201`) and never load the full file. That's enough to score columns.

### 3.2 Score each column on two signals

1. **Header name.** Normalize it (`"Mobile Phone #"` → `mobilephone`) and match it against a synonym list.
2. **The values.** What share of the sample values are valid phone numbers (§4)? This catches columns named `Contact`, `Number 2`, `WhatsApp`, or in other languages.

```ts
const norm = (h: string) => h.toLowerCase().replace(/[^a-z0-9]/g, '');
const PHONE_WORDS: Record<string, number> = { mobile: 1, cell: 0.9, whatsapp: 0.9, phone: 0.9, number: 0.6, contact: 0.5, tel: 0.6 };
const NOT_PHONE = ['fax', 'pager', 'telex', 'idnumber', 'zip', 'postal'];

function scorePhoneColumns(headers: string[], sample: string[][], country: CountryCode) {
  return headers.map((h, i) => {
    const n = norm(h);
    const nameScore = NOT_PHONE.some(w => n.includes(w)) ? 0
      : Math.max(0, ...Object.entries(PHONE_WORDS).filter(([w]) => n.includes(w)).map(([, s]) => s));
    const values = sample.map(r => r[i]?.trim()).filter(Boolean) as string[];
    const valid = values.filter(v => normalizePhone(v, country).ok).length;
    const validPct = values.length ? valid / values.length : 0;
    return { index: i, header: h, score: 0.4 * nameScore + 0.6 * validPct, validPct };
  }).sort((a, b) => b.score - a.score);
}
```

Suggest the top columns with `validPct ≥ 0.2` as a **priority list**, e.g. `Mobile Phone → Number → Business Phone`: the first valid one per row wins. Find the name the same way (`name`, `full name`, or `first name` + `last name`), and the country from `country`, `country/region` and similar.

### 3.3 Let the user confirm, then save it as a template

```ts
// Same headers (in any order) = same layout = reuse the confirmed mapping next time
const fingerprint = crypto.createHash('sha1').update(headers.map(norm).sort().join('|')).digest('hex');
await db.query(`INSERT INTO mapping_templates (account_id, fingerprint, mapping) VALUES ($1, $2, $3)
                ON CONFLICT (account_id, fingerprint) DO UPDATE SET mapping = EXCLUDED.mapping`, [acct, fingerprint, mapping]);
```

Outlook, Google Contacts and HubSpot exports always have the same headers, so after the first time they map with **zero clicks**.

### 3.4 Store the mapping by column index, and keep only what you need

```ts
type Mapping = { phones: number[]; name: number[]; country?: number; vars: [string, number][]; defaultCountry: CountryCode };
```

In the worker, read rows as **arrays** and pick `row[i]`; don't build a 90-key object per row. Only the chosen `vars` columns go into `contacts.vars` (JSONB), never all 90.

---

## 4. Phone validation: fast, cheap and correct

### 4.1 What "wrong number" can mean

| Level | Question | Tool | Cost | When |
|---|---|---|---|---|
| 1. Format | Is it a possible, valid number for its country? | `libphonenumber-js` (offline) | ~µs | **During import, every row** |
| 2. Type | Mobile or landline? | `libphonenumber-js/max`, `getType()` | ~µs | During import, if you only call mobiles |
| 3. Junk | Placeholder (`1111111111`, `1234567890`, `555-01xx`)? | small rules | ~µs | During import |
| 4. Reachable | Is the line active right now, and which carrier? | HLR / Lookup API (paid, slow) | 100–500 ms + money | **After import**, only for numbers about to be dialed, in bulk, cached for weeks |

Levels 1–3 catch most bad data for free. Never put level 4 in the import loop.

### 4.2 The per-row pipeline (cheapest checks first)

```ts
import { parsePhoneNumberFromString, CountryCode } from 'libphonenumber-js/max';

const NON_DIGIT = /\D/g;
const FAST_IN = /^(?:91|0)?([6-9]\d{9})$/;                  // your most common country: skip the parser
const SCI = /^\s*[+-]?\d+(?:\.\d+)?e[+-]?\d+\s*$/i;        // Excel's 9.19E+11: the digits are already lost
const JUNK = /^(\d)\1+$/;                                  // 0000000000, 9999999999
type Result = { ok: true; e164: string } | { ok: false; reason: 'empty' | 'invalid' | 'sci_notation' | 'junk' };
const cache = new Map<string, Result>();                   // the same raw value appears many times in big lists

export function normalizePhone(raw: string | undefined, country: CountryCode): Result {
  const s = (raw ?? '').trim();
  if (!s) return { ok: false, reason: 'empty' };
  const key = country + '|' + s;
  const hit = cache.get(key);
  if (hit) return hit;

  let res: Result;
  if (SCI.test(s)) res = { ok: false, reason: 'sci_notation' };
  else {
    let digits = s.replace(NON_DIGIT, '');
    const explicit = s.startsWith('+') || s.startsWith('00');
    if (s.startsWith('00')) digits = digits.slice(2);
    const m = (!explicit && country === 'IN') || (explicit && digits.length === 12 && digits.startsWith('91'))
      ? FAST_IN.exec(digits) : null;
    if (JUNK.test(digits)) res = { ok: false, reason: 'junk' };
    else if (m) res = { ok: true, e164: '+91' + m[1] };   // ~90% of Indian lists end here
    else {
      let pn = parsePhoneNumberFromString(explicit ? '+' + digits : s, explicit ? undefined : country);
      if (!pn?.isValid() && !explicit && digits.length >= 11) pn = parsePhoneNumberFromString('+' + digits); // dropped '+'
      res = pn?.isValid() ? { ok: true, e164: pn.number } : { ok: false, reason: 'invalid' };
    }
  }
  if (cache.size < 200_000) cache.set(key, res);
  return res;
}
```

The rules that matter most:
- **Normalize to E.164 (`+919876543210`) before anything else.** Duplicates and validity are only meaningful on the normalized form; `098765 43210` and `+91 98765 43210` are the same number.
- **Use the row's country** (from a country column, else the account default) for numbers without `+`. A UK `07775 559513` is *also* a valid Indian mobile, so without the country it silently becomes a wrong number.
- **Try several phone columns in priority order;** the first valid one wins.
- **Keep the rejected rows with a reason** (`empty`, `invalid`, `sci_notation`, `junk`) so users can fix them.
- **Cost:** a fast path plus `libphonenumber-js` is tens of microseconds per number, so 20k rows validate in well under a second on one thread. You don't need worker threads until hundreds of thousands of rows (then use `piscina` or BullMQ sandboxed processors).

---

## 5. Duplicates: the optimal way

**Let the database do it, in one batched statement, against a unique index.** Never run a `SELECT` per row.

```sql
-- once
ALTER TABLE contacts ADD CONSTRAINT contacts_campaign_phone_uq UNIQUE (campaign_id, phone_e164);
```

```ts
// per batch of ~5,000 valid rows, already normalized (verified on Postgres 16 with node-pg)
const r = await client.query(
  `INSERT INTO contacts (campaign_id, import_id, phone_e164, name, vars)
   SELECT $1::bigint, $2::bigint, p, n, v::jsonb FROM unnest($3::text[], $4::text[], $5::text[]) AS t(p, n, v)
   ON CONFLICT (campaign_id, phone_e164) DO NOTHING`,
  [campaignId, importId, phones, names, varsJson]);
const inserted = r.rowCount ?? 0;          // new contacts
const duplicates = phones.length - inserted; // already in the campaign, or repeated in this file
```

What this one statement handles:
- **Duplicates already in the database:** skipped by `ON CONFLICT DO NOTHING`.
- **Duplicates inside the batch:** also skipped, and the first occurrence wins. Verified: a batch with one existing number, one repeated number and two new numbers inserts exactly 2.
- **Duplicates across batches:** the next batch's `ON CONFLICT` catches them.
- **No 65,535-parameter limit:** the arrays are passed as 3 parameters, however many rows they hold.

Other duplicate needs:
- **Global do-not-call list or cross-campaign dedupe:** filter with `WHERE NOT EXISTS (SELECT 1 FROM dnc d WHERE d.phone_e164 = t.p)`, with an index on `dnc(phone_e164)`.
- **"Last row wins"** (update the name or vars of an existing, not-yet-called contact): use `ON CONFLICT … DO UPDATE SET name = EXCLUDED.name WHERE contacts.status = 'pending'`. But first dedupe the batch in JS (`Map` by phone), because `DO UPDATE` errors if the same key appears twice in one statement.

For 100k+ rows, `COPY` into a temp table and then `INSERT … SELECT … ON CONFLICT` is about 2–5× faster again (`pg-copy-streams`). For 10–20k rows, `unnest` is plenty and needs no extra library.

---

## 6. Recommended BullMQ + Postgres design (single server)

```
upload ─stream→ disk/S3 ─→ INSERT imports(status=queued) ─→ queue.add('import', { importId })   ← ONE job per file
worker (concurrency 1–2, its own process)
  stream-parse CSV (arrays) → batch 5,000 rows → validate (sync loop) → ONE transaction:
      INSERT contacts (unnest … ON CONFLICT DO NOTHING)
      INSERT import_errors (unnest …)
      UPDATE imports SET checkpoint_row, counters
  → job.updateProgress once per batch → yield to event loop → next batch
```

```ts
import { Worker, Job, Queue } from 'bullmq';
import { Pool } from 'pg';
import fs from 'node:fs';
import { parse } from 'csv-parse';

const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 4 });
const BATCH = 5_000;
export const importQueue = new Queue('csv-import', { connection });

// enqueue once per file
await importQueue.add('import', { importId }, {
  jobId: `import-${importId}`,                        // the same import can't be queued twice
  attempts: 3, backoff: { type: 'exponential', delay: 10_000 },
  removeOnComplete: 1000, removeOnFail: 5000,
});

new Worker('csv-import', async (job: Job<{ importId: number }>) => {
  const imp = await loadImport(job.data.importId);    // file_path, mapping, campaign_id, checkpoint_row
  const parser = fs.createReadStream(imp.file_path)
    .pipe(parse({ bom: true, relax_column_count: true, skip_empty_lines: true, from_line: 2 }));
  let rowNo = 0, batch: { rowNo: number; rec: string[] }[] = [];
  for await (const rec of parser as AsyncIterable<string[]>) {
    rowNo++;
    if (rowNo <= imp.checkpoint_row) continue;        // a retry resumes after the last committed batch
    batch.push({ rowNo, rec });
    if (batch.length === BATCH) {
      await flush(imp, batch, rowNo, job);
      batch = [];
      await new Promise(r => setImmediate(r));        // let BullMQ renew its lock (avoids "stalled" re-runs)
    }
  }
  if (batch.length) await flush(imp, batch, rowNo, job);
  await pool.query(`UPDATE imports SET status='done', finished_at=now() WHERE id=$1`, [imp.id]);
}, { connection, concurrency: 1, lockDuration: 120_000 });

async function flush(imp: Import, batch: { rowNo: number; rec: string[] }[], lastRow: number, job: Job) {
  const m = imp.mapping, phones: string[] = [], names: string[] = [], vars: string[] = [];
  const badRow: number[] = [], badRaw: (string | null)[] = [], badWhy: string[] = [];
  for (const { rowNo, rec } of batch) {
    const country = (m.country !== undefined && toCountry(rec[m.country])) || m.defaultCountry;
    let res: Result = { ok: false, reason: 'empty' }, firstRaw: string | null = null;
    for (const i of m.phones) {                       // priority list: the first valid one wins
      res = normalizePhone(rec[i], country);
      if (res.ok) break;
      if (firstRaw === null && rec[i]?.trim()) firstRaw = rec[i];
    }
    if (res.ok) {
      phones.push(res.e164);
      names.push(m.name.map(i => rec[i]?.trim()).filter(Boolean).join(' '));
      vars.push(JSON.stringify(Object.fromEntries(m.vars.map(([k, i]) => [k, rec[i] ?? '']))));
    } else { badRow.push(rowNo); badRaw.push(firstRaw); badWhy.push(res.reason); }
  }
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query('SET LOCAL synchronous_commit = off'); // safe: the checkpoint commits with the data
    const r = await client.query(
      `INSERT INTO contacts (campaign_id, import_id, phone_e164, name, vars)
       SELECT $1::bigint, $2::bigint, p, n, v::jsonb FROM unnest($3::text[], $4::text[], $5::text[]) AS t(p, n, v)
       ON CONFLICT (campaign_id, phone_e164) DO NOTHING`, [imp.campaign_id, imp.id, phones, names, vars]);
    if (badRow.length) await client.query(
      `INSERT INTO import_errors (import_id, row_no, raw_phone, reason)
       SELECT $1::bigint, r, p, w FROM unnest($2::int[], $3::text[], $4::text[]) AS t(r, p, w)
       ON CONFLICT DO NOTHING`, [imp.id, badRow, badRaw, badWhy]);
    const inserted = r.rowCount ?? 0;
    await client.query(
      `UPDATE imports SET checkpoint_row=$2, valid_rows=valid_rows+$3, duplicate_rows=duplicate_rows+$4,
              invalid_rows=invalid_rows+$5 WHERE id=$1`,
      [imp.id, lastRow, inserted, phones.length - inserted, badRow.length]);
    await client.query('COMMIT');
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  await job.updateProgress({ row: lastRow });         // once per batch, not per row
}
```

Why each piece matters:
- **One job per file, with a stable `jobId`:** no per-row Redis traffic, and a double click doesn't queue the import twice.
- **Checkpoint in the same transaction:** if the worker crashes or BullMQ retries, the job resumes after the last committed batch, and no row is inserted twice or skipped.
- **`await setImmediate` between batches plus `lockDuration`:** a long synchronous loop can't block BullMQ's lock renewal, which would trigger a "stalled" re-run.
- **A separate queue for imports with `concurrency: 1–2`:** imports can't starve your other BullMQ jobs, and vice versa.

---

## 7. A single server with "a lot of big processes"

| Check | Command | Healthy |
|---|---|---|
| CPU saturated? | `uptime`, `top` | load average < number of cores |
| Swapping? | `free -m`, `vmstat 1` (`si`/`so` columns) | swap in/out ≈ 0. Swapping makes everything 10–100× slower |
| Postgres connections | `SELECT count(*) FROM pg_stat_activity;` | well below `max_connections`. Sum of all app pool sizes ≤ ~80% of it |
| Postgres memory | `SHOW shared_buffers;` | ~25% of RAM. The default 128 MB is too small for big tables |
| Redis memory | `redis-cli INFO memory` | stable, no eviction. Use `removeOnComplete` on every queue |
| Disk | `iostat -x 1` | `%util` not pinned at 100% |

Also:
- Run the import worker as **its own process** (pm2 or systemd), not inside the API, with `concurrency` 1–2.
- Lower its priority (`nice -n 10`) if user-facing requests matter more.
- Set `max_wal_size = 2–4GB` so bulk inserts don't force constant checkpoints.

---

## 8. Expected results after the fix

| Rows | Processing time (single server, DB on the same machine) |
|---|---|
| 10k–20k | **~0.5–1.5 s** (parse + validate + 2–4 batched inserts) |
| 100k | ~3–5 s |
| 1M | ~30–60 s (switch to `COPY` for the biggest gain) |

For comparison, this repo's Python implementation measured 100k rows in about 2.6–3.5 s and 1M in about 28–33 s on 2–4 cores, including validation; see `docs/benchmarks.md`. Node with the design above lands in the same range.

## 9. Fix order (biggest win first)

1. **Replace per-row inserts or ORM saves with one `unnest … ON CONFLICT DO NOTHING` per 5k rows** (~50× faster on its own).
2. **Delete the per-row duplicate `SELECT`;** add the `UNIQUE (campaign_id, phone_e164)` index.
3. **One BullMQ job per file;** progress updates once per batch.
4. **Remove any external lookup API from the import;** do it later, only for numbers you'll dial, in bulk, cached.
5. **Stream-parse to arrays, with the mapping applied by column index;** keep only the mapped columns.
6. **Yield between batches and set `lockDuration`** (no stalled re-runs); give imports their own queue and worker process.
7. **Add the checkpoint to the batch transaction** (safe retries), mapping templates (zero-click repeat imports) and the per-row validation cache.
