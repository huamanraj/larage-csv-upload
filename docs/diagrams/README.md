# Diagrams (Eraser diagram-as-code)

Paste each file into [Eraser](https://app.eraser.io) using the diagram type named on its first line.

| File | Eraser diagram type | Shows |
|---|---|---|
| `1-high-level.cloud.eraser` | Cloud Architecture | Whole system: user, API, storage, pluggable queue, workers, database, calling layer |
| `2-upload-and-mapping.flow.eraser` | Flow Chart | Streaming upload, idempotency, preview scoring, mapping |
| `3-pluggable-queue.cloud.eraser` | Cloud Architecture | Queue-agnostic interface: Postgres / Celery / SQS / Kafka |
| `4-chunking-and-validation.flow.eraser` | Flow Chart | 10k-row chunking and per-row phone validation |
| `5-save-chunk-exactly-once.sequence.eraser` | Sequence Diagram | One chunk transaction, lease guard and crash recovery |
| `6-database-schema.erd.eraser` | Entity Relationship | Tables, keys and relationships |
| `7-fetch-and-export.flow.eraser` | Flow Chart | Progress polling, keyset pages, streamed exports |

If an icon doesn't render, pick another one from Eraser's icon list. The diagram itself still works.
