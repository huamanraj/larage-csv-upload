# A3. A Postgres-backed queue library instead of a hand-rolled claim loop

> **Verdict:** the same architecture as the chosen design, with the claim, retry and heartbeat code owned by a library instead of us. It's a strong contender, and the most likely "phase 2" if the team distrusts ~120 lines of custom queue code.

## Options (by language)

| Library | Language | Mechanism | Notes |
|---|---|---|---|
| **Procrastinate** | Python (sync + async) | `SKIP LOCKED` + `LISTEN/NOTIFY` | Django/psycopg3 integration, retries, locks, queueing locks, periodic tasks |
| **PGMQ** | Postgres extension (any language via SQL) | SQS-like API with visibility timeouts | Needs the extension installed; managed PG support varies |
| **pg-boss / graphile-worker** | Node.js | `SKIP LOCKED` | Relevant only if the worker is written in Node |
| **River** | Go | `SKIP LOCKED` + transactional enqueue | Transactional enqueue is a big plus |
| **Oban** | Elixir | `SKIP LOCKED` | The reference design many others copy |

## How it works

Same as the chosen design, with the claim loop replaced:

```
api ── confirm mapping ─▶ BEGIN; UPDATE imports SET status='queued'; enqueue('import', id); COMMIT
                                              (transactional enqueue: job exists iff mapping committed)
library worker ─▶ picks job (SKIP LOCKED) ─▶ our chunk loop (unchanged)
```

## Sketch (Procrastinate)

```python
app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=DATABASE_URL))

@app.task(queue="imports", retry=procrastinate.RetryStrategy(max_attempts=5, wait=10),
          queueing_lock="import")          # at most one queued import job per lock
def import_file(import_id: int):
    Job(conn, pool, load(import_id)).run()  # same chunk transaction + checkpoint
```

Run with `procrastinate worker --queues imports --concurrency 1`.

## Pros

- **No new infrastructure.** It's still one Postgres.
- **Transactional enqueue** (in most of these libraries): the job is enqueued in the same transaction as the business write. No outbox is needed.
- **Less custom code:** retries with backoff, stuck-job handling, periodic tasks (the retention sweep), admin CLI/UI, and `LISTEN/NOTIFY` wake-ups instead of 1 s polling.
- A good base if we add more job types later, without adopting a broker.

## Cons

- A library dependency and its conventions: schema migrations, its own tables, upgrade cadence.
- **It doesn't remove our checkpoint logic.** The library guarantees *the job* runs at least once. *Chunk* idempotency is still ours: the checkpoint in the same transaction plus the ownership guard.
- Some libraries' "stalled job" detection is time-based, like ours. Long chunks need a heartbeat either way.
- PGMQ requires a Postgres extension. Not all managed providers allow it.
- The team has to learn one more abstraction on top of a fairly simple SQL pattern.

## Cost

Zero infrastructure cost. The only cost is a few days of integration.

## When to choose it

- The review is uncomfortable owning queue code.
- We're adding 2 or more other background job types and want one consistent mechanism without Redis.
- We want periodic tasks (retention, `ANALYZE`) managed in the same place.

## Migration from the chosen design

It takes about a day:
1. Add the library schema.
2. Enqueue on mapping confirmation.
3. Delete `claim()` and `slot_loop()`.
4. Keep `Job.run()` / `flush()` exactly as they are.

The UI, the tables and the fetch endpoints are unchanged.
