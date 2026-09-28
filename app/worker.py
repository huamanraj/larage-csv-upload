"""Import worker: takes queued imports from Postgres (FOR UPDATE SKIP LOCKED), streams the CSV
in 10k-row chunks, validates them in a process pool and saves each chunk in one transaction.

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
from .phones import validate_batch

log = logging.getLogger("worker")
STOP = threading.Event()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
csv.field_size_limit(16 * 1024 * 1024)


class LostLock(Exception):
    """Another worker re-claimed this import after our lock went stale."""


# Take the oldest queued import (or one whose worker died) without blocking on rows others hold.
CLAIM_SQL = f"""
UPDATE imports SET status='processing', locked_at=now(), worker_id=%(worker)s, started_at=coalesce(started_at, now())
WHERE id = (SELECT id FROM imports
            WHERE status='queued'
               OR (status='processing' AND locked_at < now() - interval '{config.LOCK_TIMEOUT}')
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
        cur.execute("SELECT pg_advisory_xact_lock(7332)")  # serialize claims so the concurrency cap holds
        cur.execute(f"SELECT count(*) AS n FROM imports WHERE status='processing' "
                    f"AND locked_at >= now() - interval '{config.LOCK_TIMEOUT}'")
        if cur.fetchone()["n"] >= config.IMPORT_CONCURRENCY:
            return None
        cur.execute(CLAIM_SQL, {"worker": WORKER_ID})
        job = cur.fetchone()
        if job:
            cur.execute("SELECT count(*) AS n FROM import_events WHERE import_id=%s AND kind='chunk'", (job["id"],))
            job["chunks_done"] = cur.fetchone()["n"]
            event(cur, job["id"], "claimed", {"worker": WORKER_ID, "resume_from": job["checkpoint_row"]})
        return job


class Job:
    def __init__(self, conn, pool, job):
        self.conn, self.pool, self.job = conn, pool, job
        self.id, self.checkpoint, self.n = job["id"], job["checkpoint_row"], job["chunks_done"]

    def run(self):
        t_start = time.monotonic()
        with open(self.job["file_path"], newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            header = [h.strip().lower() for h in next(reader, [])]
            col = {h: i for i, h in reversed(list(enumerate(header)))}  # first occurrence wins
            if "phone" not in col:
                raise RuntimeError("CSV must have a 'phone' column")
            p, nm, ct = col["phone"], col.get("name"), col.get("country")
            extra = [(h, i) for i, h in enumerate(header) if i not in (p, nm, ct) and h]
            with self.conn.cursor() as cur:
                event(cur, self.id, "opened", {"path": self.job["file_path"], "bytes": self.job["file_size"]})

            def cell(rec, i):
                return rec[i].strip() if i is not None and i < len(rec) else ""

            row_no, chunk, t_read = 0, [], time.monotonic()
            for rec in reader:                            # streaming: one row in memory at a time
                row_no += 1
                if row_no <= self.checkpoint or not rec:  # already saved before a restart
                    continue
                vars_json = json.dumps({h: cell(rec, i) for h, i in extra if cell(rec, i)}) if extra else None
                chunk.append((row_no, cell(rec, p), cell(rec, nm), cell(rec, ct), vars_json))
                if len(chunk) >= config.CHUNK_SIZE:
                    self.flush(chunk, row_no, time.monotonic() - t_read)
                    if STOP.is_set():
                        return self.release()
                    chunk, t_read = [], time.monotonic()
            if chunk or row_no > self.checkpoint:
                self.flush(chunk, row_no, time.monotonic() - t_read)
        self.finish(time.monotonic() - t_start)

    def flush(self, chunk, last_row, read_s):
        # Validate: split the chunk across the process pool (cores - 1).
        t0 = time.monotonic()
        k = max(1, min(config.POOL_SIZE, len(chunk) // 1000))
        size = -(-len(chunk) // k) if chunk else 1
        ok, bad = [], []
        for o, b in self.pool.map(validate_batch, [chunk[i:i + size] for i in range(0, len(chunk), size)],
                                  [config.DEFAULT_REGION] * k):
            ok += o
            bad += b
        t1 = time.monotonic()

        # Save: one transaction for data + checkpoint, so a crash can never save a chunk twice.
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit = off")
            cur.execute("UPDATE imports SET locked_at=now() WHERE id=%s AND worker_id=%s "
                        "AND status='processing' AND checkpoint_row=%s", (self.id, WORKER_ID, self.checkpoint))
            if cur.rowcount != 1:
                raise LostLock()
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
            self.n += 1
            ms = lambda s: round(s * 1000, 1)
            event(cur, self.id, "chunk", {
                "n": self.n, "first_row": self.checkpoint + 1, "last_row": last_row, "rows": len(chunk),
                "valid": len(ok), "invalid": len(bad), "inserted": inserted, "dups": len(ok) - inserted,
                "read_ms": ms(read_s), "validate_ms": ms(t1 - t0), "save_ms": ms(t2 - t1),
                "progress_ms": ms(time.monotonic() - t2)})
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_schema()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    log.info("worker %s: %s slot(s), %s validation process(es), chunk %s",
             WORKER_ID, config.IMPORT_CONCURRENCY, config.POOL_SIZE, config.CHUNK_SIZE)
    with ProcessPoolExecutor(config.POOL_SIZE, initializer=signal.signal,
                             initargs=(signal.SIGINT, signal.SIG_IGN)) as pool:
        pool.submit(int).result()  # start the pool before any threads
        threads = [threading.Thread(target=slot_loop, args=(pool,), daemon=True)
                   for _ in range(config.IMPORT_CONCURRENCY)]
        for t in threads:
            t.start()
        while not STOP.is_set():
            STOP.wait(1.0)
        for t in threads:
            t.join(timeout=60)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
