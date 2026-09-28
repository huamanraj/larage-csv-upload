"""Import worker: claims queued imports from Postgres (SKIP LOCKED) and processes them in 10k-row chunks.

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
from pathlib import Path

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
    """Another worker re-claimed this import (our lock went stale). Stop without touching it."""


CLAIM_SQL = f"""
UPDATE imports SET status='processing', locked_at=now(), worker_id=%(worker)s,
       started_at=coalesce(started_at, now()), error=NULL
WHERE id = (SELECT id FROM imports
            WHERE status='queued'
               OR (status='processing' AND locked_at < now() - interval '{config.LOCK_TIMEOUT}')
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *
"""

MERGE_SQL = """
WITH ins AS (
  INSERT INTO contacts (campaign_id, import_id, phone_e164, name, vars)
  SELECT %(campaign)s::bigint, %(import)s::bigint, phone_e164, name, vars
  FROM (SELECT DISTINCT ON (phone_e164) row_no, phone_e164, name, vars
        FROM stage ORDER BY phone_e164, row_no) first_seen   -- in-file dupes: first row wins
  ORDER BY row_no                                            -- keep file order in contacts.id
  ON CONFLICT (campaign_id, phone_e164) DO NOTHING
  RETURNING 1)
SELECT count(*) FROM ins
"""


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            return round(int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
    except OSError:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def connect() -> psycopg.Connection:
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS stage "
                 "(row_no int, phone_e164 text, name text, vars jsonb) ON COMMIT DELETE ROWS")
    return conn


def event(cur, import_id, kind, data):
    cur.execute("INSERT INTO import_events (import_id, kind, data) VALUES (%s, %s, %s)",
                (import_id, kind, json.dumps(data)))


def claim(conn):
    with conn.transaction(), conn.cursor() as cur:
        # One claimer at a time, so the global concurrency check below can't race.
        cur.execute("SELECT pg_advisory_xact_lock(7332)")
        cur.execute(f"SELECT count(*) AS n FROM imports WHERE status='processing' "
                    f"AND locked_at >= now() - interval '{config.LOCK_TIMEOUT}'")
        if cur.fetchone()["n"] >= config.IMPORT_CONCURRENCY:
            return None
        cur.execute(CLAIM_SQL, {"worker": WORKER_ID})
        job = cur.fetchone()
        if job:
            cur.execute("SELECT count(*) AS n FROM import_events WHERE import_id=%s AND kind='chunk'", (job["id"],))
            job["chunks_done"] = cur.fetchone()["n"]
            event(cur, job["id"], "claimed", {"worker": WORKER_ID, "resume_from": job["checkpoint_row"],
                                              "pool": config.POOL_SIZE, "chunk_size": config.CHUNK_SIZE})
        return job


class Job:
    def __init__(self, conn, pool, job):
        self.conn, self.pool, self.job = conn, pool, job
        self.id = job["id"]
        self.checkpoint = job["checkpoint_row"]
        self.reasons = dict(job["reject_reasons"] or {})
        self.n = job["chunks_done"]
        m = job["mapping"]
        self.phone_cols = m["phone_columns"]
        self.name_col = m.get("name_column")
        self.var_cols = m.get("var_columns") or []
        self.region = job["default_region"]
        self.reject_landline = bool(m.get("reject_landline"))

    def run(self):
        path = Path(self.job["file_path"])
        if not path.exists():
            raise RuntimeError("source file is gone (retention cleanup?)")
        t_start = time.monotonic()
        # Streaming reader: memory is bounded by one chunk regardless of file size.
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                raise RuntimeError("file has no header row")
            pos = {}
            for i, h in enumerate(header):
                pos.setdefault(h, i)
            try:
                phone_idx = [pos[c] for c in self.phone_cols]
                var_idx = [pos[c] for c in self.var_cols]
            except KeyError as e:
                raise RuntimeError(f"mapped column {e} not in file")
            name_idx = pos.get(self.name_col) if self.name_col else None

            def get(rec, i):
                return rec[i] if i is not None and i < len(rec) else ""

            row_no, chunk, t_read = 0, [], time.monotonic()
            for rec in reader:
                row_no += 1
                if row_no <= self.checkpoint:  # already committed before a crash / restart
                    continue
                if not rec:
                    continue
                chunk.append((row_no, tuple(get(rec, i) for i in phone_idx), get(rec, name_idx),
                              tuple(get(rec, i) for i in var_idx)))
                if len(chunk) >= config.CHUNK_SIZE:
                    self.flush(chunk, row_no, time.monotonic() - t_read)
                    chunk = []
                    if STOP.is_set():
                        return self.release()
                    if config.CHUNK_DELAY_MS:
                        STOP.wait(config.CHUNK_DELAY_MS / 1000)
                    t_read = time.monotonic()
            if chunk or row_no > self.checkpoint:
                self.flush(chunk, row_no, time.monotonic() - t_read)
        self.finish(time.monotonic() - t_start)

    def flush(self, chunk, last_row, read_s):
        t0 = time.monotonic()
        # Validate across the process pool (cores - 1), split into even slices.
        k = max(1, min(config.POOL_SIZE, len(chunk) // 500 or 1))
        size = -(-len(chunk) // k) if chunk else 0
        futures = [self.pool.submit(validate_batch, chunk[i:i + size], self.var_cols, self.region,
                                    self.reject_landline) for i in range(0, len(chunk), size)] if chunk else []
        ok, bad, fast, chunk_reasons = [], [], 0, {}
        for fut in futures:
            o, b, reasons, f_ = fut.result()
            ok += o
            bad += b
            fast += f_
            for r, c in reasons.items():
                chunk_reasons[r] = chunk_reasons.get(r, 0) + c
        reasons_after = {r: self.reasons.get(r, 0) + chunk_reasons.get(r, 0) for r in {*self.reasons, *chunk_reasons}}
        t1 = time.monotonic()

        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit = off")  # safe: checkpoint commits with the data
            # Lock our import row first and prove we still own it at the expected checkpoint.
            cur.execute("UPDATE imports SET locked_at=now() WHERE id=%s AND worker_id=%s "
                        "AND status='processing' AND checkpoint_row=%s", (self.id, WORKER_ID, self.checkpoint))
            if cur.rowcount != 1:
                raise LostLock()
            with cur.copy("COPY stage (row_no, phone_e164, name, vars) FROM STDIN") as cp:
                for r in ok:
                    cp.write_row(r)
            t2 = time.monotonic()
            with cur.copy("COPY import_errors (import_id, row_no, raw_phone, reason) FROM STDIN") as cp:
                for r in bad:
                    cp.write_row((self.id, *r))
            t3 = time.monotonic()
            cur.execute(MERGE_SQL, {"campaign": self.job["campaign_id"], "import": self.id})
            inserted = cur.fetchone()["count"]
            t4 = time.monotonic()
            dups = len(ok) - inserted
            cur.execute("""UPDATE imports SET checkpoint_row=%s, locked_at=now(),
                             valid_rows=valid_rows+%s, invalid_rows=invalid_rows+%s,
                             duplicate_rows=duplicate_rows+%s, reject_reasons=%s
                           WHERE id=%s""",
                        (last_row, inserted, len(bad), dups, json.dumps(reasons_after), self.id))
            self.n += 1
            ms = lambda s: round(s * 1000, 1)
            event(cur, self.id, "chunk", {
                "n": self.n, "first_row": self.checkpoint + 1, "last_row": last_row, "rows": len(chunk),
                "staged": len(ok), "inserted": inserted, "dups": dups, "rejected": len(bad), "reasons": chunk_reasons, "fast": fast,
                "workers": len(futures), "read_ms": ms(read_s), "validate_ms": ms(t1 - t0), "copy_ms": ms(t2 - t1),
                "errors_ms": ms(t3 - t2), "merge_ms": ms(t4 - t3), "rss_mb": rss_mb()})
        commit_ms = round((time.monotonic() - t4) * 1000, 1)
        self.checkpoint, self.reasons = last_row, reasons_after
        log.info("import %s chunk %s rows<=%s +%s dup=%s bad=%s commit=%sms",
                 self.id, self.n, last_row, inserted, dups, len(bad), commit_ms)

    def finish(self, elapsed):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""UPDATE imports SET status='done', finished_at=now(), locked_at=NULL
                           WHERE id=%s AND worker_id=%s AND status='processing'
                           RETURNING checkpoint_row, valid_rows, invalid_rows, duplicate_rows""",
                        (self.id, WORKER_ID))
            row = cur.fetchone()
            if not row:
                raise LostLock()
            event(cur, self.id, "done", {**row, "seconds": round(elapsed, 2)})
        if row["checkpoint_row"] >= config.ANALYZE_AFTER_ROWS:
            t = time.monotonic()
            self.conn.execute("ANALYZE contacts")
            with self.conn.cursor() as cur:
                event(cur, self.id, "analyze", {"ms": round((time.monotonic() - t) * 1000, 1)})

    def release(self):
        """Graceful shutdown: hand the job back to the queue; the checkpoint makes the resume exact."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("UPDATE imports SET status='queued', locked_at=NULL, worker_id=NULL "
                        "WHERE id=%s AND worker_id=%s", (self.id, WORKER_ID))
            event(cur, self.id, "released", {"checkpoint_row": self.checkpoint, "worker": WORKER_ID})


def fail(conn, job_id, err):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE imports SET status='failed', error=%s, locked_at=NULL, finished_at=now() "
                    "WHERE id=%s AND worker_id=%s", (err[:2000], job_id, WORKER_ID))
        event(cur, job_id, "failed", {"error": err[:500]})


def slot_loop(slot, pool):
    conn = None
    while not STOP.is_set():
        try:
            if conn is None or conn.closed:
                conn = connect()
            job = claim(conn)
            if not job:
                STOP.wait(1.0)
                continue
            log.info("slot %s claimed import %s (resume from row %s)", slot, job["id"], job["checkpoint_row"])
            try:
                Job(conn, pool, job).run()
            except LostLock:
                log.warning("import %s: lock lost to another worker, abandoning", job["id"])
            except psycopg.OperationalError:
                raise  # connection trouble: the stale lock lets it be re-claimed
            except Exception as e:  # noqa: BLE001
                log.exception("import %s failed", job["id"])
                fail(conn, job["id"], f"{type(e).__name__}: {e}")
        except psycopg.OperationalError as e:
            log.warning("db connection problem: %s", e)
            try:
                conn and conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            STOP.wait(2.0)


def retention_loop():
    """Delete raw CSVs of finished imports after RETENTION_DAYS; import_errors stay for the rejected-rows CSV."""
    while not STOP.is_set():
        try:
            with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
                rows = conn.execute("SELECT file_path FROM imports WHERE status IN ('done','failed') "
                                    "AND finished_at < now() - make_interval(days => %s)",
                                    (config.RETENTION_DAYS,)).fetchall()
            for (p,) in rows:
                Path(p).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log.warning("retention sweep failed: %s", e)
        STOP.wait(3600)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_schema()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    log.info("worker %s: %s slot(s), %s validation process(es), chunk %s",
             WORKER_ID, config.IMPORT_CONCURRENCY, config.POOL_SIZE, config.CHUNK_SIZE)
    with ProcessPoolExecutor(config.POOL_SIZE, initializer=signal.signal,
                             initargs=(signal.SIGINT, signal.SIG_IGN)) as pool:
        pool.submit(int).result()  # spawn the pool up front
        threads = [threading.Thread(target=slot_loop, args=(i, pool), daemon=True)
                   for i in range(config.IMPORT_CONCURRENCY)]
        threads.append(threading.Thread(target=retention_loop, daemon=True))
        for t in threads:
            t.start()
        while not STOP.is_set():
            STOP.wait(1.0)
        for t in threads[:-1]:
            t.join(timeout=60)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
