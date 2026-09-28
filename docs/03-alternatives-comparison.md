# 3. Alternatives compared

Scores assume **our** context: up to 1M rows per file, a few imports per hour, one server today, a small team, and phone numbers as PII (DPDP Act / customer contracts).

**Legend:** ✅ good · ➖ acceptable / depends · ❌ poor

## Matrix

| | **Chosen: PG queue + chunked COPY** | A1 pandas in request | A2 Celery + Redis | A3 PG queue library | A4 S3 + serverless | A5 In-DB ELT | A6 DuckDB/Polars | A7 Client-side parse | A8 Temporal | A9 Kafka | A10 Importer SaaS |
|---|---|---|---|---|---|---|---|---|---|---|---|
| New infra to run | none | none | Redis/RabbitMQ + Flower | none (lib/extension) | bucket + queue + functions | none | none (lib) | none | Temporal cluster/cloud | Kafka + Connect | vendor |
| Handles 1M rows / 250 MB | ✅ | ❌ RAM + timeouts | ✅ | ✅ | ✅ | ✅ | ✅ | ➖ browser-dependent | ✅ | ✅ | ➖ plan limits |
| API stays responsive | ✅ | ❌ | ✅ | ✅ | ✅ (API not involved) | ➖ COPY via API | ✅ | ✅ | ✅ | ✅ | ✅ |
| Exactly-once / resumable | ✅ by construction | ❌ | ➖ needs extra design | ✅ same pattern | ✅ with per-part/chunk checkpoints | ➖ one big txn or manual batching | ✅ if checkpointed | ➖ needs per-batch acks | ✅ built in | ✅ with transactions | ➖ vendor-defined |
| Resumable *upload* | ❌ | ❌ | ❌ | ❌ | ✅ multipart | ❌ | ❌ | ✅ (sends small batches) | ❌ | ❌ | ✅ |
| Horizontal scale | ❌ local disk | ❌ | ✅ | ➖ needs shared storage | ✅ | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ |
| Throughput (1M rows) | ~40 s *(extrapolated)* | minutes, or OOM | ~similar | ~similar | ~similar, plus cold starts | fastest (~10–20 s *est*) | faster validate step | limited by user's PC/network | ~similar, plus overhead | ~similar | vendor |
| Memory profile | constant (~50 MB) | O(file) | constant | constant | constant | in DB | O(file) unless streaming | O(batch) in browser | constant | constant | n/a |
| Live progress | ✅ | ❌ | ➖ custom | ✅ | ➖ custom | ❌ coarse | ✅ | ✅ | ✅ | ➖ | ✅ |
| PII stays in our infra | ✅ | ✅ | ✅ | ✅ | ✅ (own bucket) | ✅ | ✅ | ✅ | ✅ / ➖ cloud | ✅ | ❌ |
| Build effort | ~1–2 wks (done as skeleton) | 1 day | ~2 wks | ~1–2 wks | ~2–3 wks | ~1–2 wks | +3–5 days on top | ~2 wks + server | ~3 wks + learning | ~4+ wks | days + integration |
| Ops burden | lowest | lowest | medium | low | medium (cloud) | low | low | low | high | very high | low (vendor) |
| Running cost | ~0 extra | 0 | small VM/managed Redis | 0 | pay per use | 0 | 0 | 0 | cluster or per-action | high | per-import/seat fees |
| Lock-in | none | none | low | low | cloud provider | Postgres | none | none | Temporal | Kafka | high |

## How to read this

- **A1 is the baseline to avoid.** It's here so nobody reinvents it under deadline pressure.
- **A3 and A6 are refinements *of* the chosen design,** not competitors. We can adopt either later without changing the data model or the chunk transaction.
- **A4 is the natural next step** once we need resumable uploads or more than one node. It replaces the *upload and storage* half and keeps the *processing* half.
- **A2, A8 and A9 solve problems we don't have yet:** a broker for many task types, durable multi-step workflows, event streams. Each adds a stateful system to operate.
- **A5** is the fastest raw path, but it moves validation logic into SQL, and the `phonenumbers` fallback doesn't exist there.
- **A7 and A10** move the mapping and validation UX to the client. A7 is a legitimate upgrade for upload size. A10 is a business decision about PII and cost.

## Recommendation

1. **Ship the chosen approach now.** It meets every hard requirement, including exactly-once, with **zero new infrastructure**, and it's already built and crash-tested.
2. **Before customer launch,** fix the medium-risk items in [02](02-downsides-and-risks.md): encoding sniffing, formula-injection escaping, full rows in rejects, and size-aware queue priority.
3. **Pre-agree the triggers** for moving to A4 (and optionally A3); see [04 debate guide](04-debate-guide.md#triggers-to-revisit-the-decision).
