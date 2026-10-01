# Benchmarks: time, CPU, RAM and DB load by VM size

Whole-system numbers: the API, the worker (with its validation processes) and Postgres all run on the same N-core machine, and everything they use is counted. Nothing is shown as a delta from zero.

## How this was measured

- **Machine:** 4-vCPU Linux VM, 16 GB RAM, SSD.
- **Postgres:** 16, configured like `docker-compose.yml`: `shared_buffers=1GB`, `max_wal_size=4GB`, `checkpoint_timeout=15min`.
- **"2-core" run:** Postgres, the API, the worker and its validation processes were all pinned to 2 CPUs with `taskset`.
- **"4-core" run:** everything used all 4 CPUs.
- **"8-core":** a *projection* (see below), not a measurement.
- **Data:** `scripts/make_sample.py`, mostly Indian numbers with about 8% foreign ones. About 62% of rows are new contacts, 29% are duplicates and 9% are invalid.
  - An all-foreign file makes validation about 2× slower.
  - Wide Outlook files (92 columns) make reading slower.
- **Setup per run:** the database is reset and checkpointed first. The file is uploaded over localhost, so upload time over a real network is **not** included.
- **Sampling:** CPU and memory are sampled every 0.5 s by a process running on a separate CPU. Memory is PSS, which splits shared memory fairly, so Postgres's shared buffers are not counted several times.
  - "cores" means CPU-seconds per second: 1.0 is one core fully busy.
  - Peaks are 0.5 s averages. Within a chunk, validation bursts briefly reach the full core count; the in-app CPU chart shows those per-step bursts.
- **Re-running:** `python scripts/bench_system.py --cores 2,4,8`. Run it on your own VM to replace the projection with real numbers.

## Processing time (upload → all rows saved)

| Rows | CSV size | 2 cores | 4 cores | 8 cores *(projected)* |
|---|---|---|---|---|
| 100k | 5.0 MB | 3.52 s | 2.59 s | ~2.3 s |
| 200k | 9.9 MB | 6.76 s | 5.13 s | ~4.6 s |
| 500k | 24.8 MB | 16.88 s | 12.98 s | ~11.7 s |
| 1M | 49.6 MB | 33.22 s | 27.57 s | ~25.1 s |

Throughput is about 30k rows/s on 2 cores and about 36k rows/s on 4 cores. Time grows linearly with rows, because every chunk costs the same.

## CPU (cores in use, average / peak over 0.5 s)

| Rows | Cores | Worker | Postgres | API | **Whole machine** |
|---|---|---|---|---|---|
| 100k | 2 | 0.41 / 0.69 | 0.31 / 0.65 | 0.02 / 0.17 | **0.83 / 1.18** |
| 100k | 4 | 0.49 / 0.85 | 0.28 / 0.54 | 0.02 / 0.19 | **0.94 / 1.65** |
| 200k | 2 | 0.46 / 0.7 | 0.38 / 0.62 | 0.02 / 0.25 | **0.97 / 1.24** |
| 200k | 4 | 0.54 / 0.86 | 0.39 / 0.58 | 0.02 / 0.27 | **1.1 / 1.39** |
| 500k | 2 | 0.49 / 0.76 | 0.42 / 0.62 | 0.02 / 0.61 | **1.02 / 1.15** |
| 500k | 4 | 0.61 / 0.82 | 0.45 / 0.69 | 0.02 / 0.61 | **1.18 / 1.38** |
| 1M | 2 | 0.5 / 0.7 | 0.45 / 0.66 | 0.02 / 0.64 | **1.05 / 1.15** |
| 1M | 4 | 0.62 / 0.81 | 0.51 / 0.71 | 0.02 / 0.64 | **1.24 / 1.48** |

**How to read this:** one import keeps about **1–1.3 cores busy on average**, whatever the VM size. Read → validate → save run one after another: the worker parses (1 core), then validates (a short burst on all its cores), then waits while Postgres saves (about 0.5 core). A bigger VM makes the validation burst shorter; it doesn't make the import use the whole machine. The spare cores stay free for the rest of your app, or for running more imports in parallel (`IMPORT_CONCURRENCY`).

## RAM (PSS, MB): idle before the import → peak during it

| Rows | Cores | Worker | Postgres | API | Whole VM used |
|---|---|---|---|---|---|
| 100k | 2 | 33 → 85 | 132 → 145 | 48 → 48 | 781 → 841 |
| 100k | 4 | 37 → 102 | 133 → 161 | 48 → 48 | 889 → 989 |
| 200k | 2 | 84 → 89 | 143 → 145 | 48 → 48 | 832 → 843 |
| 200k | 4 | 102 → 106 | 160 → 190 | 48 → 48 | 984 → 1017 |
| 500k | 2 | 85 → 108 | 143 → 165 | 48 → 49 | 831 → 900 |
| 500k | 4 | 106 → 107 | 177 → 213 | 48 → 49 | 951 → 972 |
| 1M | 2 | 108 → 126 | 164 → 247 | 49 → 49 | 858 → 1004 |
| 1M | 4 | 106 → 112 | 211 → 248 | 49 → 49 | 949 → 1028 |

- **Worker:** about 30 MB per validation process plus about 30 MB for the main process. It's bounded by the chunk size, not the file size: 1M rows needs about as much as 100k. On 8 cores, expect about **180 MB**, because the pool starts one process per CPU.
- **Postgres:** grows as `shared_buffers` fill with the contacts table and its indexes, capped by `shared_buffers` (1 GB here). About 250 MB after 1M rows. It stays allocated after the import, because that's Postgres's cache.
- **API:** about 50 MB, flat. The upload streams to disk.
- **Whole VM used:** everything on the VM, including the OS. Plan on **≥ 2 GB RAM** for API + worker + Postgres with a 1 GB `shared_buffers`, or lower `shared_buffers` on smaller VMs.

## Database load ("DB spikes")

| Rows | New contacts | WAL written | Peak WAL per chunk | DB time per chunk, avg / peak (2c) | Same (4c) | Checkpoints during import | DB growth |
|---|---|---|---|---|---|---|---|
| 100k | 61,580 | 29 MB | 4.13 MB | 155 / 219 ms | 128 / 166 ms | 0 | +16 MB |
| 200k | 122,862 | 59 MB | 4.22 MB | 158 / 211 ms | 143 / 198 ms | 0 | +33 MB |
| 500k | 307,071 | 147 MB | 4.25 MB | 168 / 249 ms | 145 / 208 ms | 0 | +82 MB |
| 1M | 615,453 | 294 MB | 4.25 MB | 169 / 241 ms | 162 / 376 ms | 0 | +164 MB |

- **What a spike is:** each 10k-row chunk is one transaction, about 4 MB of WAL written in 130–250 ms, and then nothing until the next chunk. That's the burst pattern in the in-app "DB writes" chart.
- **WAL per contact:** about 500 bytes per *new* contact (row + 3 indexes). Duplicates and invalid rows add almost nothing.
- **No checkpoint during an import:** with `max_wal_size=4GB`, even 1M rows (about 300 MB of WAL) doesn't force one. The data files are written later by the background checkpointer. With a small `max_wal_size` (the default is 1 GB), expect a checkpoint about every 2–3M rows, which shows up as a slower chunk.
- **No temp files:** the per-chunk sort fits in `work_mem`.
- **Disk to budget per 1M-row import:** about 165 MB of table and index growth, about 300 MB of WAL (recycled), plus the uploaded CSV (about 50 MB, kept until reset or cleanup).

## Database on another machine (RDS / managed Postgres)

Measured by sending the worker's database traffic through a proxy that adds a fixed network delay (200k rows, 4 cores). The proxy with no delay adds nothing measurable: 5.02 s vs 4.91 s direct.

| Round-trip time to the DB | Typical setup | 200k rows | DB time per chunk | vs same VM |
|---|---|---|---|---|
| ~0 ms | Postgres on the same VM | 4.91 s | 135 ms | – |
| 1 ms | RDS in the same availability zone | 5.67 s | 169 ms | +15% |
| 5 ms | RDS in another zone / nearby region | 6.48 s | 198 ms | +32% |
| 20 ms | DB in a far region | 10.65 s | 370 ms | +117% |

Each 10k-row chunk makes about 12 round trips (guard, COPY ×2, merge, checkpoint, event, WAL metrics, commit), so remote-DB cost ≈ **12 × RTT per 10k rows**. Keep the worker in the same zone as the database. Two of those round trips are only for the WAL metric, and could be dropped, or the statements pipelined, if latency matters.

## 8-core projection (how it's derived)

Per chunk at 4 cores: read ≈ 65 ms (one process, doesn't scale) + validate ≈ 44 ms (3 parallel slices) + DB save ≈ 160 ms (Postgres, mostly one core). With 8 cores, validation runs in 7 slices, about 20 ms, saving about 24 ms per chunk (~9%). Read and save stay about the same. The API's and Postgres's memory are unchanged; the worker grows to about 180 MB (8 validation processes). Average CPU stays about 1.2–1.3 cores per import.

To get real 8-core numbers, run `python scripts/bench_system.py --cores 2,4,8` on an 8-vCPU VM.

## What this means for your deployment

| VM | One 1M-row import | What else fits |
|---|---|---|
| 2 vCPU / 2–4 GB | ~33 s. It uses about half the machine on average, so your API stays responsive | Keep `IMPORT_CONCURRENCY=1` |
| 4 vCPU / 4–8 GB | ~28 s. About 1.2 of 4 cores busy | `IMPORT_CONCURRENCY=2` is safe |
| 8 vCPU / 8–16 GB | ~25 s *(projected)* | 3–4 imports in parallel before cores run out |

**Measured: parallel imports** (4 vCPU, 300k rows per file, wall time including uploads):
- 1 file: 8.4 s.
- 2 in parallel: 10.5 s, about 57k rows/s total.
- 4 in parallel: 19.8 s, about 61k rows/s, with the CPU full.
- 4 into the same campaign, one by one: 34.6 s.

Worker RAM stays ~160 MB in every case. The live system panel in the UI shows the same thing for your own files; see the README, "Parallel imports".

Beyond 4 cores, a single import barely gets faster. Extra cores buy **more imports in parallel**, not a faster single import. Making one import faster needs pipelining (read the next chunk while the current one saves) or cutting the per-row Python work; see the discussion in the docs.

## Raw data

`docs/benchmarks.jsonl` holds every metric for each run, one JSON object per line.
