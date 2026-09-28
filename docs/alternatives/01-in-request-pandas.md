# A1. Parse inside the request (pandas / DictReader in the handler)

> **Verdict:** avoid. It's included as the baseline, because it's what gets built under deadline pressure, and it fails at exactly the file sizes we care about.

## How it works

```
POST /upload (multipart)
  └─ file = await request.form()          # whole file buffered (RAM or temp file)
  └─ df = pandas.read_csv(file)           # whole file parsed into memory
  └─ df["phone"] = df["Mobile Phone"].map(normalize)
  └─ Contact.objects.bulk_create(...)     # or session.add_all(...) / executemany
  └─ return {"imported": n}
```

Variants include moving the same code into `BackgroundTasks` or a thread, or chunking with `read_csv(chunksize=...)`.

## Sketch

```python
@app.post("/upload")
async def upload(file: UploadFile):
    df = pd.read_csv(file.file, dtype=str)                 # 250 MB → ~1–2.5 GB of Python objects
    df["phone_e164"] = df["Mobile Phone"].map(normalize)   # single core, GIL
    rows = df.dropna(subset=["phone_e164"]).to_dict("records")
    await db.executemany("INSERT INTO contacts ... ON CONFLICT DO NOTHING", rows)
    return {"imported": len(rows)}
```

## Pros

- The smallest amount of code. It can be written in an afternoon.
- It's easy to understand and debug for small files.
- There's no queue, worker or extra process.

## Cons

| Problem | Impact at 1M rows / 250 MB |
|---|---|
| Memory | pandas with `dtype=str` across 93 columns uses several times the file size in RAM (*estimate* 1–2.5 GB). Two concurrent imports can OOM the API. |
| Request timeouts | Parsing, validating and inserting takes minutes, while proxies usually cut at 60–100 s. The client sees an error, but the server may still be inserting, so the retry creates duplicates or a race. |
| API starvation | CPU-bound work in the API process blocks the event loop or GIL, so every other endpoint gets slow. |
| Insert speed | `executemany` or ORM `bulk_create` is far slower than COPY (roughly 50–120 s against 5–10 s per 1M rows in common benchmarks). |
| No progress | The user stares at a spinner. |
| No resumability | A crash at 80% leaves 800k rows inserted, no record of where it stopped, and a retry re-processes everything. |
| No idempotency | Double-submit means double work. The unique index saves correctness, but not time. |
| Stores all columns | It's tempting to dump the whole `df` into jsonb, which means 93 columns of PII per contact. |

## "But with `chunksize` and `BackgroundTasks`…"

`read_csv(chunksize=10000)` inside a `BackgroundTasks` job fixes the memory problem. It still:
- runs in the API process (CPU contention)
- dies with the API process on deploy or restart, and nothing re-claims it
- has no checkpoint unless you add one, and once you do, you've rebuilt the chosen design without its guarantees

## When it's acceptable

- Internal tools with files under about 10k rows and one user.
- One-off admin scripts run from a shell, not a request.

## Migration from here to the chosen design

This is the typical "v0 that fell over". The migration steps:

1. Stream the upload to disk.
2. Add an `imports` table with a status column.
3. Move the loop into a worker process.
4. Swap inserts for COPY into a staging table plus a merge.
5. Commit a checkpoint per chunk.

That's the chosen approach.
