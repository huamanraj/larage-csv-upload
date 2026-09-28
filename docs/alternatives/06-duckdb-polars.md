# A6. Vectorised processing with DuckDB or Polars

> **Verdict:** an *engine swap inside the worker*, not an architectural alternative. It could make the read and validate steps several times faster. It doesn't touch our real bottleneck (the unique-index merge), and it brings memory and CSV-leniency trade-offs. Adopt it only if profiling shows validation dominating.

## How it works

The queue, upload and chunk transaction stay the same. What changes is how a chunk is produced:

```
worker ─▶ DuckDB: read_csv('/data/imports/42.csv', all_varchar=true, ...)  (parallel, streaming scan)
        ─▶ SQL/expressions: regexp_replace + regexp_extract fast path, COALESCE over priority columns
        ─▶ rows that fail the fast path → Python phonenumbers (small remainder)
        ─▶ fetch in 10k-row record batches (Arrow) ─▶ COPY into Postgres stage ─▶ same merge + checkpoint
```

## Sketch (DuckDB)

```python
con = duckdb.connect()
rel = con.sql(f"""
  SELECT row_number() OVER () AS row_no, "First Name" AS name,
         COALESCE(
           regexp_extract(regexp_replace("Mobile Phone",  '\\D', '', 'g'), '^(?:91|0)?([6-9]\\d{{9}})$', 1),
           regexp_extract(regexp_replace("Primary Phone", '\\D', '', 'g'), '^(?:91|0)?([6-9]\\d{{9}})$', 1)
         ) AS national,
         "Mobile Phone" AS raw1, "Primary Phone" AS raw2
  FROM read_csv('{path}', all_varchar=true, header=true, ignore_errors=true)
""")
reader = rel.fetch_record_batch(10_000)       # Arrow batches, bounded memory
for batch in reader:
    ...  # leftovers (national == '') → phonenumbers; then COPY batch to Postgres, merge, checkpoint
```

Polars works the same way with `pl.scan_csv(...)`: the lazy/streaming engine, `str.replace_all`, and `str.extract`.

## Pros

- **Parsing and normalisation are multi-threaded native code.**
  - Reading plus regex on 1M rows is typically a few seconds (*estimate*).
  - This replaces both `csv.reader` and the process pool.
- **Less Python code for the fast path.** It's a declarative expression instead of a per-row function.
- **Robust CSV sniffing** (delimiter, quote, header detection) and `ignore_errors` for malformed lines.
- It's easy to add column profiling to the preview (fill rate and distinct counts per column).

## Cons

- **The merge is still the bottleneck.** In our measurements, validation was about 30–50 ms per 10k chunk and the merge was 100–200 ms. Making validation 5× faster saves roughly 15% end to end.
- **Memory.** DuckDB streams, but its parallel CSV reader buffers. Polars' eager `read_csv` loads everything, so we'd have to be disciplined about the lazy/streaming APIs.
- **Row numbers and resume.**
  - We need a stable `row_no` for error reporting and for skipping rows `<= checkpoint_row`.
  - Parallel readers can reorder rows unless ordering is preserved (DuckDB's `preserve_insertion_order`, which is the default and costs some parallelism).
  - Resume means re-scanning with a `WHERE row_no > checkpoint` filter.
- **`ignore_errors` silently drops rows.** That's convenient, but a dropped row never shows up in `import_errors` unless we add explicit handling (DuckDB's `store_rejects` option helps).
- **A new native dependency** in the worker image (a large wheel), with a separate threading model that competes with the process pool if both are used.
- **The `phonenumbers` fallback is still Python.** A UDF path works but is slow, so keep the leftovers in a separate loop.

## When to choose it

- Profiling on real customer files shows read and validate above ~50% of chunk time. That would happen with, for example, many foreign numbers or 20+ phone-candidate columns.
- We add richer per-column profiling to the mapping step.
- We move to much larger files (tens of millions of rows).

## Migration from the chosen design

It stays within `app/worker.py`:
1. Replace the `csv.reader` loop and `validate_batch` pool with a DuckDB relation that yields Arrow batches.
2. Keep `flush()` (COPY, merge, checkpoint) as it is.

This can go behind a feature flag and be A/B tested on the same files.
