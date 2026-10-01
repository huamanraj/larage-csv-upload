"""Import worker: takes queued imports from Postgres (FOR UPDATE SKIP LOCKED), streams the CSV
in 10k-row chunks, validates them in a process pool and saves each chunk in one transaction.

IMPORT_CONCURRENCY slots run in parallel (one thread each, sharing the validation pool), so several files
are processed at once. Two imports into the same campaign never run together: they would race on its
duplicate check, so the second waits in the queue.

Run as its own process:  python -m app.worker
"""
import csv
import json
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import psycopg
from psycopg.rows import dict_row

from . import config
from .db import init_schema
from .columns import resolve
from .metrics import ALLOWED_CPUS, CORES, RAM_TOTAL_MB, SystemSampler, Usage, pin, tree_pids
from .phones import validate_batch

log = logging.getLogger("worker")
STOP = threading.Event()
RUNNING = set()  # import ids this worker is processing right now
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
csv.field_size_limit(16 * 1024 * 1024)


class LostLock(Exception):
    """Another worker re-claimed this import after our lock went stale."""


LIVE = f"status='processing' AND locked_at >= now() - interval '{config.LOCK_TIMEOUT}'"

# Take the oldest queued import (or one whose worker died) without blocking on rows others hold,
# skipping imports whose campaign already has a live import (one import per campaign at a time).
CLAIM_SQL = f"""
UPDATE imports SET status='processing', locked_at=now(), worker_id=%(worker)s, started_at=coalesce(started_at, now())
WHERE id = (SELECT id FROM imports i
            WHERE (status='queued'
                   OR (status='processing' AND locked_at < now() - interval '{config.LOCK_TIMEOUT}'))
              AND NOT EXISTS (SELECT 1 FROM imports o WHERE o.campaign_id = i.campaign_id AND o.id <> i.id AND o.{LIVE})
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *
"""

# In-file duplicates: first row wins. Existing contacts: ON CONFLICT skips them.
MERGE_SQL = """
WITH ins AS (
  INSERT INTO contacts (campaign_id, import_id, phone_e164, name, vars)
  SELECT %(campaign)s::bigint, %(import)s::bigint, phone_e164, name, vars
  FROM (SELECT DISTINCT ON (phone_e164) * FROM stage ORDER BY phone_e164, row_no) first_seen
  ORDER BY row_no
  ON CONFLICT (campaign_id, phone_e164) DO NOTHING
  RETURNING 1)
SELECT count(*) AS n FROM ins
"""


def event(cur, import_id, kind, data):
    cur.execute("INSERT INTO import_events (import_id, kind, data) VALUES (%s, %s, %s)",
                (import_id, kind, json.dumps(data)))


def connect():
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS stage "
                 "(row_no int, phone_e164 text, name text, vars jsonb) ON COMMIT DELETE ROWS")
    return conn


def claim(conn):
    with conn.transaction(), conn.cursor() as cur:
        # Serialize claims, so the concurrency cap and the one-import-per-campaign rule both hold.
        cur.execute("SELECT pg_advisory_xact_lock(7332)")
        cur.execute(f"SELECT count(*) AS n FROM imports WHERE {LIVE}")
        running = cur.fetchone()["n"]
        if running >= config.IMPORT_CONCURRENCY:
            return None
        cur.execute(CLAIM_SQL, {"worker": WORKER_ID})
        job = cur.fetchone()
        if job:
            cur.execute("SELECT count(*) AS n FROM import_events WHERE import_id=%s AND kind='chunk'", (job["id"],))
            job["chunks_done"] = cur.fetchone()["n"]
            event(cur, job["id"], "claimed", {"worker": WORKER_ID, "resume_from": job["checkpoint_row"],
                                              "cores": CORES, "ram_mb": RAM_TOTAL_MB, "running": running + 1,
                                              "slots": config.IMPORT_CONCURRENCY})
        return job


class Job:
    def __init__(self, conn, pool, job):
        self.conn, self.pool, self.job = conn, pool, job
        self.id, self.checkpoint, self.n = job["id"], job["checkpoint_row"], job["chunks_done"]
        self.slices = config.POOL_SIZE  # parallel validation slices per chunk

    def limit_cores(self):
        """Apply the import's core cap: validate in N slices and, with a single slot, also pin the worker and
        its validation processes to the first N CPUs. With several slots the worker is shared by other
        imports, so it is not pinned. The cap never covers Postgres."""
        cap = self.job.get("cores")
        if not cap:
            return {"cap": None, "cores_used": len(ALLOWED_CPUS), "slices": self.slices, "pinned": False}
        n = max(1, min(cap, len(ALLOWED_CPUS)))
        self.slices = n
        if config.IMPORT_CONCURRENCY > 1:
            return {"cap": cap, "cores_used": n, "slices": n, "pinned": False}
        pinned = pin(tree_pids(), ALLOWED_CPUS[:n])
        return {"cap": cap, "cores_used": n, "cpus": ALLOWED_CPUS[:n], "slices": n, "pinned": pinned}

    def run(self):
        RUNNING.add(self.id)
        try:
            self._run()
        finally:
            RUNNING.discard(self.id)
            if self.job.get("cores") and config.IMPORT_CONCURRENCY == 1:
                pin(tree_pids(), ALLOWED_CPUS)  # give the worker all CPUs back for the next import

    def _run(self):
        limits = self.limit_cores()
        t_start = time.monotonic()
        with open(self.job["file_path"], newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            cols = resolve(next(reader, []))
            if not cols["phones"]:
                raise RuntimeError("no phone column found")
            phones, names, countries, extra = cols["phones"], cols["name"], cols["country"], cols["extra"]
            self.fields = tuple((h, cols["kinds"].get(i)) for h, i in extra)  # extra columns, e-mail/date checked
            extra_idx = [i for _, i in extra]
            with self.conn.cursor() as cur:
                event(cur, self.id, "opened", {
                    "path": self.job["file_path"], "bytes": self.job["file_size"], **limits,
                    "slots": config.IMPORT_CONCURRENCY,
                    "checked": {k: [h for h, kk in self.fields if kk == k] for k in ("email", "date")}})
            self.usage = Usage()  # CPU and RAM of this worker + its validation processes

            def cell(rec, i):
                return rec[i].strip() if i is not None and i < len(rec) else ""

            row_no, chunk, t_read = 0, [], time.monotonic()
            for rec in reader:                            # streaming: one row in memory at a time
                row_no += 1
                if row_no <= self.checkpoint or not rec:  # already saved before a restart
                    continue
                name = " ".join(v for i in names if (v := cell(rec, i)))
                country = next((v for i in countries if (v := cell(rec, i))), "")
                chunk.append((row_no, tuple(cell(rec, i) for i in phones), name, country,
                              tuple(cell(rec, i) for i in extra_idx)))
                if len(chunk) >= config.CHUNK_SIZE:
                    self.flush(chunk, row_no, time.monotonic() - t_read)
                    if STOP.is_set():
                        return self.release()
                    chunk, t_read = [], time.monotonic()
            if chunk or row_no > self.checkpoint:
                self.flush(chunk, row_no, time.monotonic() - t_read)
        self.finish(time.monotonic() - t_start)

    def flush(self, chunk, last_row, read_s):
        cpu_read, _ = self.usage.tick()  # since the previous chunk: reading and parsing rows
        # Validate: split the chunk across the process pool (cores - 1).
        t0 = time.monotonic()
        k = max(1, min(self.slices, len(chunk) // 1000))
        size = -(-len(chunk) // k) if chunk else 1
        ok, bad, reasons, blanked = [], [], {}, {"email_blanked": 0, "date_blanked": 0}
        for o, b, st in self.pool.map(validate_batch, [chunk[i:i + size] for i in range(0, len(chunk), size)],
                                      [config.DEFAULT_REGION] * k, [self.fields] * k):
            ok += o
            bad += b
            for r, c in st["reasons"].items():
                reasons[r] = reasons.get(r, 0) + c
            for f in blanked:
                blanked[f] += st[f]
        t1 = time.monotonic()
        cpu_validate, rss_validate = self.usage.tick()

        # Save: one transaction for data + checkpoint, so a crash can never save a chunk twice.
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit = off")
            cur.execute("UPDATE imports SET locked_at=now() WHERE id=%s AND worker_id=%s "
                        "AND status='processing' AND checkpoint_row=%s", (self.id, WORKER_ID, self.checkpoint))
            if cur.rowcount != 1:
                raise LostLock()
            wal0 = cur.execute("SELECT pg_current_wal_insert_lsn() AS l").fetchone()["l"]
            with cur.copy("COPY stage (row_no, phone_e164, name, vars) FROM STDIN") as cp:
                for r in ok:
                    cp.write_row(r)
            with cur.copy("COPY import_errors (import_id, row_no, raw_phone, reason) FROM STDIN") as cp:
                for r in bad:
                    cp.write_row((self.id, *r))
            cur.execute(MERGE_SQL, {"campaign": self.job["campaign_id"], "import": self.id})
            inserted = cur.fetchone()["n"]
            t2 = time.monotonic()
            cur.execute("""UPDATE imports SET checkpoint_row=%s, locked_at=now(), valid_rows=valid_rows+%s,
                             invalid_rows=invalid_rows+%s, duplicate_rows=duplicate_rows+%s WHERE id=%s""",
                        (last_row, inserted, len(bad), len(ok) - inserted, self.id))
            # WAL bytes this chunk made the database write (whole cluster; the import dominates).
            wal = cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s) AS b", (wal0,)).fetchone()["b"]
            cpu_save, rss_save = self.usage.tick()
            self.n += 1
            ms = lambda s: round(s * 1000, 1)
            event(cur, self.id, "chunk", {
                "n": self.n, "first_row": self.checkpoint + 1, "last_row": last_row, "rows": len(chunk),
                "valid": len(ok), "invalid": len(bad), "inserted": inserted, "dups": len(ok) - inserted,
                "reasons": reasons, **blanked, "running": len(RUNNING),
                "read_ms": ms(read_s), "validate_ms": ms(t1 - t0), "save_ms": ms(t2 - t1),
                "progress_ms": ms(time.monotonic() - t2),
                "cpu_read": cpu_read, "cpu_validate": cpu_validate, "cpu_save": cpu_save,
                "rss_mb": max(rss_validate, rss_save), "wal_mb": round(float(wal) / 2**20, 2)})
        self.checkpoint = last_row

    def finish(self, elapsed):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""UPDATE imports SET status='done', finished_at=now(), locked_at=NULL
                           WHERE id=%s AND worker_id=%s AND status='processing'
                           RETURNING checkpoint_row, valid_rows, invalid_rows, duplicate_rows""", (self.id, WORKER_ID))
            row = cur.fetchone()
            if not row:
                raise LostLock()
            event(cur, self.id, "done", {**row, "seconds": round(elapsed, 2)})

    def release(self):
        """Graceful stop: put the job back in the queue; the checkpoint makes the resume exact."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("UPDATE imports SET status='queued', locked_at=NULL, worker_id=NULL WHERE id=%s AND worker_id=%s",
                        (self.id, WORKER_ID))
            event(cur, self.id, "released", {"checkpoint_row": self.checkpoint})


def fail(conn, job_id, err):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE imports SET status='failed', error=%s, locked_at=NULL, finished_at=now() "
                    "WHERE id=%s AND worker_id=%s", (err[:2000], job_id, WORKER_ID))
        event(cur, job_id, "failed", {"error": err[:500]})


def slot_loop(pool):
    conn = None
    while not STOP.is_set():
        try:
            if conn is None or conn.closed:
                conn = connect()
            job = claim(conn)
            if not job:
                STOP.wait(0.5)
                continue
            log.info("claimed import %s (resume from row %s)", job["id"], job["checkpoint_row"])
            try:
                Job(conn, pool, job).run()
            except LostLock:
                log.warning("import %s: lock lost to another worker", job["id"])
            except psycopg.OperationalError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("import %s failed", job["id"])
                fail(conn, job["id"], f"{type(e).__name__}: {e}")
        except psycopg.OperationalError as e:
            log.warning("db connection problem: %s", e)
            conn = None
            STOP.wait(2.0)


def sample_loop():
    """Every 0.5 s: CPU and memory of this worker (+ its validation processes), Postgres (when its processes are
    visible, i.e. not in another container), the whole machine, and the database's write counters.
    The UI draws these as the live system charts. Rows older than 30 minutes are pruned."""
    sampler, conn, last_prune = SystemSampler("worker", watch_postgres=True), None, 0.0
    while not STOP.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
            data = sampler.tick()
            data["running"] = sorted(RUNNING)
            data["pg"] = conn.execute(
                """SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '0/0')::bigint AS wal,
                          tup_inserted AS ins, xact_commit AS commits FROM pg_stat_database
                   WHERE datname = current_database()""").fetchone()
            conn.execute("INSERT INTO system_samples (source, data) VALUES ('worker', %s)", (json.dumps(data),))
            if time.monotonic() - last_prune > 60:
                conn.execute("DELETE FROM system_samples WHERE at < now() - interval '30 min'")
                last_prune = time.monotonic()
        except psycopg.Error as e:
            log.warning("sampler: %s", e)
            conn = None
        STOP.wait(SystemSampler.EVERY)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_schema()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    procs = max(config.POOL_SIZE, len(ALLOWED_CPUS))  # enough processes for a cap of every available CPU
    log.info("worker %s: %s slot(s), %s validation process(es) (%s slices when uncapped), chunk %s",
             WORKER_ID, config.IMPORT_CONCURRENCY, procs, config.POOL_SIZE, config.CHUNK_SIZE)
    with ProcessPoolExecutor(procs, initializer=signal.signal,
                             initargs=(signal.SIGINT, signal.SIG_IGN)) as pool:
        pool.submit(int).result()  # start the pool before any threads
        threads = [threading.Thread(target=slot_loop, args=(pool,), daemon=True)
                   for _ in range(config.IMPORT_CONCURRENCY)]
        threading.Thread(target=sample_loop, daemon=True).start()
        for t in threads:
            t.start()
        while not STOP.is_set():
            STOP.wait(1.0)
        for t in threads:
            t.join(timeout=60)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
