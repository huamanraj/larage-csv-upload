# A8. A durable workflow engine (Temporal, or similar: Restate, AWS Step Functions)

> **Verdict:** excellent durability and visibility for **multi-step, long-running, human-in-the-loop** processes. For a single import pipeline it's heavy infrastructure. Consider it when imports become one step of a larger flow (import → enrich → DNC scrub → approval → schedule calls).

## How it works

```
api ── mapping confirmed ─▶ temporal.start_workflow(ImportWorkflow, import_id)
Temporal server (history in its own DB) ─▶ worker(s) run activities:
   ImportWorkflow:
      meta = activity(read_header_and_count)
      for chunk in range(meta.chunks):                       # deterministic workflow code
          activity(process_chunk, import_id, chunk, retry=...)   # COPY + merge + checkpoint in PG
      activity(finalize, import_id)
      [later steps: activity(dnc_scrub), wait_for_signal("approved"), activity(schedule_calls)]
```

## Sketch (Python SDK)

```python
@workflow.defn
class ImportWorkflow:
    @workflow.run
    async def run(self, import_id: int):
        meta = await workflow.execute_activity(scan_file, import_id, start_to_close_timeout=timedelta(minutes=5))
        for i in range(meta.chunks):
            await workflow.execute_activity(process_chunk, args=[import_id, i],
                                            start_to_close_timeout=timedelta(minutes=2),
                                            heartbeat_timeout=timedelta(seconds=30),
                                            retry_policy=RetryPolicy(maximum_attempts=10))
        await workflow.execute_activity(finalize, import_id, start_to_close_timeout=timedelta(minutes=1))
```

## Pros

- **Durable execution.** The workflow survives worker crashes and deploys, with automatic retries, timeouts and heartbeats. Most of the queue edge cases we hand-wrote come for free.
- **Great visibility.** The Temporal UI shows every workflow, every activity attempt, inputs and outputs, and stack traces.
- **Composable long flows.** Wait days for an approval signal, call external APIs with retries, and compensate on failure (sagas).
- **Scales horizontally,** with task-queue routing (for example, a "big files" queue).

## Cons

- **Heavy infrastructure.** You either run a Temporal cluster (server plus a persistence database plus optional Elasticsearch) or pay for Temporal Cloud. Neither is small next to "one Postgres".
- **A learning curve.**
  - Workflow determinism rules (no I/O, no randomness, no wall-clock time in workflow code).
  - Versioning workflows when code changes.
  - Activity versus workflow boundaries.
- **Exactly-once for the data is still ours.** Activities are at-least-once, so `process_chunk` must be idempotent. That means the same Postgres checkpoint and guard as the chosen design.
- **History size limits.** A 100-chunk workflow is fine. Much larger fan-outs need `continue_as_new` or child workflows.
- **Latency overhead** per activity: small, but measurable across hundreds of chunks.
- **Progress for our UI** still needs counters in our database, or queries against Temporal's API.

## Cost

- **Self-hosted:** a few extra services, plus the operational knowledge to run them.
- **Temporal Cloud:** billed per action or storage. It's modest at our volume, but it's a new vendor and another place PII metadata could appear (keep payloads to ids only).

## When to choose it

- Imports become part of a multi-step business process with waits, approvals and external calls.
- The company already runs Temporal for other domains.
- We need auditability of every retry and step.

## Migration from the chosen design

- `Job.flush()` becomes the `process_chunk` activity, which is already idempotent via the checkpoint and guard.
- The claim loop and heartbeat code are deleted, because Temporal owns them.
- Tables, UI and fetch endpoints are unchanged.
