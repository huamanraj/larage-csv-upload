# A2. Celery (or RQ / Dramatiq / Arq) with a Redis or RabbitMQ broker

> **Verdict:** it works, and it's familiar to many Python teams. For this pipeline it adds a broker and a results backend to operate, and it makes exactly-once *harder*, not easier. It's reasonable if the company already runs Celery for many job types.

## How it works

```
api ── upload to disk/S3 ─▶ INSERT imports ─▶ celery.send_task("import_file", id)
                                                     │
                           Redis / RabbitMQ broker ◀─┘
                                     │
               celery worker(s) ─────┘
                  ├─ option 1: one long task streams the file, chunk by chunk
                  └─ option 2: fan-out: one task per chunk (group/chord) + a finaliser
```

## Sketch

```python
@app.task(bind=True, acks_late=True, max_retries=5)
def import_file(self, import_id):
    imp = load(import_id)
    for chunk in stream_chunks(imp.path, start=imp.checkpoint_row):
        with db.transaction():
            copy_stage(chunk); merge(); update_checkpoint(chunk.last_row)
    mark_done(import_id)

# fan-out variant
chord(process_chunk.s(import_id, start, end) for start, end in ranges(imp))(finalize.s(import_id))
```

## Pros

- A mature ecosystem: retries, rate limits, routing, beat schedules, and Flower for monitoring.
- A natural home for **other** background jobs (emails, enrichment, call scheduling) if the product grows many of them.
- The fan-out variant can spread one big file across several machines.
- Many engineers already know it.

## Cons

### Exactly-once is the hard part

- A task is "done" when the broker gets an ack. The data is "done" when Postgres commits. These are two systems.
  - Ack before commit, then crash: the chunk is lost.
  - Commit before ack, then crash: the chunk is redelivered.
- You still need the **checkpoint in Postgres** (as in the chosen design) to make redelivery harmless. So the broker ends up carrying a message, but the correctness still comes from Postgres.
- With the Redis broker plus `acks_late`, tasks that run longer than `visibility_timeout` (default 1 h) are **redelivered to a second worker while the first is still running**. A 1M-row import on a busy box can approach that. Without an ownership guard, that's double processing.
- The fan-out variant needs byte-range splitting of the CSV, which is tricky with quoted newlines. It also needs a chord/finaliser, which is known for rough edges around failed members and result-backend load, plus per-chunk dedupe ordering rules.

### Operational cost

- Redis or RabbitMQ has to be deployed, monitored, persisted (AOF/RDB), secured and upgraded.
- It usually needs a result backend too (Redis or a database).
- There's another place for "stuck" state to hide: the job is `processing` in Postgres, but gone from the broker.

### Other

- **Progress:** Celery task state isn't a good progress bar. We'd still write counters to Postgres.
- **Concurrency caps:** a global limit of one import at a time needs a dedicated queue with `concurrency=1`, or a lock.

## Cost

- A small managed Redis instance or VM, plus Flower. It's modest in money, but not zero in attention.

## When to choose it

- The company already runs Celery in production, with on-call knowledge.
- We're about to add several other job types, so a shared task system pays for itself.
- We need to spread a *single* import across machines (fan-out).

## Migration from the chosen design

It's easy and non-destructive:
1. Wrap `Job.run()` in a Celery task.
2. Replace the claim loop with `send_task` on mapping confirmation.
3. **Keep** the Postgres checkpoint and the `worker_id` ownership guard. They're what make redelivery safe.

Everything else (the chunk transaction, the UI, the fetch endpoints) stays the same.
