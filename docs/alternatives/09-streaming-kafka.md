# A9. Streaming platform (Kafka / Redpanda / Kinesis)

> **Verdict:** overkill. A CSV import is a bounded batch with a clear start and end, owned by one user, and it needs per-file progress and a per-file error report. Streaming platforms are built for unbounded event flows with many producers and consumers. Consider one only if contacts must fan out in real time to several downstream systems.

## How it works

```
api ── upload file ─▶ producer task reads CSV ─▶ topic "contact-rows" (key = campaign:phone)
                                                     │  partitions (e.g. 12)
consumers (validator group) ─▶ normalise ─▶ topic "contacts-valid" / "contacts-rejected"
sink (Kafka Connect JDBC or custom consumer) ─▶ batch upsert into Postgres
progress: count per import_id via a stream processor or counters table
```

## Pros

- **Massive horizontal scale** in partitions and consumer groups, with backpressure built in.
- **Replayable log.** You can re-process a file's rows with new rules by resetting offsets, within retention.
- **Fan-out.** Other systems (CRM sync, analytics, DNC service) can consume the same validated stream independently.
- Keying by `campaign:phone` routes duplicates to the same partition, so a consumer can dedupe locally.

## Cons

- **Heavy operations.** Brokers, a KRaft/ZooKeeper quorum, schema registry, Connect workers, monitoring, and partition rebalancing. Managed offerings reduce the work but add cost.
- **Batch semantics are awkward.**
  - "Is import 42 finished?" needs end-of-file markers per partition and a coordinator.
  - "How many rows were rejected?" needs a stateful stream job or a counters table.
- **Exactly-once is achievable but complex.** It takes idempotent producers, transactional producers and consumers, and a transactional sink (or idempotent upserts plus offset commits in the same database transaction). That's more intricate than one Postgres transaction per chunk.
- **Throughput isn't better for our case.** The sink still does `INSERT … ON CONFLICT` into the same unique index, which is the same bottleneck.
- **More PII copies.** Rows sit in topic retention, replicated three times, in addition to Postgres. That's harder to reason about for deletion and retention requirements.
- The team needs Kafka expertise to run and debug it.

## Cost

- The highest of all options in both money (clusters or managed throughput units) and people time.

## When to choose it

- Contacts must stream to several consumers in near real time.
- Ingestion is continuous (webhooks, partner feeds) rather than user-uploaded files.
- The company already runs Kafka and has a platform team for it.

## Migration from the chosen design

It's possible later without disruption. Add an **outbox**: the chunk transaction also writes new `contacts` ids to an outbox table, and a relay (e.g. Debezium CDC) publishes them to Kafka. The import itself stays batch and transactional, and downstream systems get a stream.
