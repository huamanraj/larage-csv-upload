# A5. In-database ELT: COPY the raw file, validate and merge in SQL

> **Verdict:** the fastest raw path and very little Python. But validation moves into SQL, the `phonenumbers` fallback isn't available there, all columns land in staging, and fine-grained progress and resume need batching we'd have to build anyway. It's a good fit for clean, uniform feeds, and a weaker fit for messy human uploads.

## How it works

```
upload ─▶ header read ─▶ CREATE UNLOGGED TABLE raw_{id} (c1 text, c2 text, ... c93 text)
        ─▶ COPY raw_{id} FROM STDIN (FORMAT csv, HEADER)        -- whole file, streamed from API or file
        ─▶ one SQL statement (or batches by row-number range):
              normalize with regexp_replace / regex match
              DISTINCT ON + INSERT ... ON CONFLICT DO NOTHING into contacts
              INSERT INTO import_errors ... for rows that failed
        ─▶ DROP TABLE raw_{id}
```

On RDS or Aurora, `aws_s3.table_import_from_s3(...)` can load straight from a bucket without the file passing through the app.

## Sketch

```sql
CREATE UNLOGGED TABLE raw_42 (row_no bigserial, "Mobile Phone" text, "Primary Phone" text, "First Name" text, ...);
COPY raw_42 ("Mobile Phone", "Primary Phone", "First Name", ...) FROM STDIN WITH (FORMAT csv, HEADER);

WITH norm AS (
  SELECT row_no, "First Name" AS name,
         COALESCE(
           (regexp_match(regexp_replace("Mobile Phone",  '\D', '', 'g'), '^(?:91|0)?([6-9]\d{9})$'))[1],
           (regexp_match(regexp_replace("Primary Phone", '\D', '', 'g'), '^(?:91|0)?([6-9]\d{9})$'))[1]
         ) AS national
  FROM raw_42 WHERE row_no BETWEEN $lo AND $hi        -- batching for progress/resume
)
INSERT INTO contacts (campaign_id, import_id, phone_e164, name)
SELECT DISTINCT ON (national) $cid, 42, '+91' || national, name
FROM norm WHERE national IS NOT NULL ORDER BY national, row_no
ON CONFLICT (campaign_id, phone_e164) DO NOTHING;
```

## Pros

- **Raw speed.**
  - COPY of a 250 MB CSV into an UNLOGGED table typically takes 10–20 s (*estimate*).
  - Set-based SQL normalisation is fast.
  - There's no Python per-row cost and no process pool.
- **Very little application code.** The whole pipeline is a few SQL statements.
- The app does no data movement if you use `aws_s3` import or a server-side file.
- Easy ad-hoc analysis of the raw file while it sits in staging ("how many rows have a blank Mobile?").

## Cons

- **No `phonenumbers` in SQL.**
  - Foreign numbers, odd formats and landline typing need either a PL/Python function (`plpython3u` is untrusted and usually **not available on managed Postgres**) or a second pass in the app for leftovers. The second pass reintroduces the worker.
- **All columns land in staging.** That's 93 text columns of PII written to the database, even temporarily. UNLOGGED tables aren't crash-safe and aren't replicated.
- **Dynamic DDL per import.** Header names become column identifiers, so every name must be quoted and escaped carefully, including duplicate headers, empty headers and 63-byte identifier limits.
- **Malformed CSV fails the whole COPY.** One bad row aborts it, unlike Python's lenient reader.
  - Postgres 17 adds `ON_ERROR ignore` for type errors, but structural problems (a wrong column count) still fail.
- **Progress and resume are coarse.** You get "copying…" and then "merging…". Fine-grained progress and crash-resume require batching by `row_no` ranges with a checkpoint, which is our chunk loop again, just in SQL.
- **The bottleneck doesn't go away.** The merge into `UNIQUE (campaign_id, phone_e164)` costs the same as in the chosen design, and it was already our largest step.
- **Database load.** Heavy regex over 1M rows runs on the primary database, which serves the rest of the product. Our design puts that CPU on the worker instead.

## When to choose it

- The input is machine-generated and uniform (a partner feed), not a hand-edited Excel export.
- Numbers are domestic and the regex path covers nearly everything.
- Throughput matters more than per-row error reporting.

## Hybrid worth considering

Keep the chosen design, and let the worker handle only the *leftovers* with `phonenumbers`:
1. COPY the raw file into staging.
2. Run the fast-path regex in SQL.
3. Fetch only the rows that failed the regex.

This can be tried later without changing the schema.
