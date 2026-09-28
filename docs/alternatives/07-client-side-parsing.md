# A7. Client-side parsing: the browser reads the CSV and sends only mapped columns

> **Verdict:** a legitimate upgrade when *upload size or reliability* is the pain point. It shrinks the data sent by about 5–8× and gives an instant preview. The costs are that the tab has to stay open for the whole import, performance depends on the user's device, and the server must still re-validate everything.

## How it works

```
browser
  ├─ File API + Papa Parse (in a Web Worker), streaming, chunked
  ├─ preview + column scoring locally (first N rows)          ← instant, no upload yet
  ├─ user confirms mapping
  ├─ POST /imports {campaign_id, file_sha256, est_rows, mapping} → import_id
  └─ for each 10k-row batch (only mapped columns, as JSON/NDJSON/CSV):
        POST /imports/{id}/batches/{seq}   (idempotent on (import_id, seq))
            server: validate (authoritative) → COPY stage → merge → checkpoint=seq
        on network error: retry the same seq
```

## Sketch

```js
// browser (web worker)
let seq = 0, batch = [];
Papa.parse(file, {
  header: true, worker: true, skipEmptyLines: true,
  chunk: async (res, parser) => {
    for (const r of res.data) batch.push([r[m.phone1], r[m.phone2], r[m.name], ...m.vars.map(v => r[v])]);
    if (batch.length >= 10000) { parser.pause(); await send(++seq, batch); batch = []; parser.resume(); }
  },
  complete: () => send(++seq, batch, /*last*/ true),
});
```

```python
# server: idempotent batch endpoint
@app.post("/api/imports/{id}/batches/{seq}")
async def batch(id: int, seq: int, rows: list[list[str]]):
    # same transaction as today; the guard makes retries safe
    # UPDATE imports SET ... WHERE id=$id AND checkpoint_seq = $seq - 1
    ...
```

## Pros

- **Much less data over the wire.** 5 of 93 columns means roughly 30–40 MB instead of 250 MB (*estimate*). That matters on mobile networks.
- **Resumable by design.** Each batch is small and idempotent on `(import_id, seq)`. A dropped connection retries one batch.
- **Instant preview and mapping.** There's no waiting for the upload before seeing columns.
- **The raw file never reaches our servers,** so fewer PII columns are stored. That's good for data minimisation.
- The server does no disk I/O and no raw-file retention.

## Cons

- **The tab has to stay open.**
  - If the user closes the laptop or navigates away, the import pauses. It can be resumed only if they reopen and re-select the same file (matched by hash).
  - Server-side processing, by contrast, finishes on its own.
- **Device performance varies.**
  - Parsing 250 MB in a Web Worker is fine on a modern laptop and slow on old hardware or phones.
  - Hashing 250 MB in the browser (SubtleCrypto) isn't streaming, so it needs a chunked JS SHA-256 library.
- **We can't trust the client.**
  - The server must re-validate every phone and enforce the mapping's column count.
  - A malicious client can send arbitrary rows. That's no worse than a crafted CSV, but the validation code now lives in two places (a JS fast path for preview, Python for the authoritative result).
- **Encoding and CSV quirks are handled in the browser.** Papa Parse is good, but behaviour differs from Python's `csv`, which can surprise support.
- **No raw file for audit or re-processing.** If the mapping was wrong, the user has to re-upload. (With server-side storage we could re-run with a new mapping.)
- **Throughput is limited by the client's upload rate and request round-trips,** not by our server.

## When to choose it

- Users are on slow or unreliable links (field sales, mobile).
- We want to minimise PII stored server-side (no raw file at all).
- Upload time dominates total import time.

## Migration from the chosen design

It can run in parallel:
1. Add the batch endpoint, reusing `flush()` with the ownership guard replaced by a `seq` guard.
2. Keep the file-upload path for API clients and very old browsers.

The UI already has the mapping screen. It would just be fed by local parsing.
