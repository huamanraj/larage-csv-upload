"""HTTP API + static UI.  Run: uvicorn app.main:app

Accepted CSV layouts (header names, case-insensitive), see app/columns.py:
  simple:  phone (required), name, country, + any other columns
  Outlook: Mobile Phone / Number / Primary Phone / Business Phone ..., First/Last Name, Country/Region
Numbers without a country code are read in the row's country, else DEFAULT_REGION.
Other non-empty columns are kept in contacts.vars.
"""
import asyncio
import csv
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from . import config
from .columns import resolve
from .db import init_schema
from .metrics import ALLOWED_CPUS, CORES, RAM_TOTAL_MB, SystemSampler, Usage

STATIC = Path(__file__).parent / "static"
SAMPLE_EVERY = 1 / 60  # record ~60 upload progress samples for the timeline
pool: AsyncConnectionPool = None  # opened in lifespan
log = logging.getLogger("api")


async def sample_loop():
    """CPU and memory of this API process (busy while files upload) for the live system charts."""
    sampler = SystemSampler("api")
    while True:
        await asyncio.sleep(SystemSampler.EVERY)
        try:
            data = await asyncio.to_thread(sampler.tick)
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO system_samples (source, data) VALUES ('api', %s)", (json.dumps(data),))
        except Exception as e:  # noqa: BLE001  (never let sampling take the API down)
            log.warning("sampler: %s", e)


@asynccontextmanager
async def lifespan(_app):
    global pool
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    init_schema()
    pool = AsyncConnectionPool(config.DATABASE_URL, min_size=1, max_size=10, open=False,
                               kwargs={"autocommit": True, "row_factory": dict_row})
    await pool.open()
    sampler = asyncio.create_task(sample_loop())
    yield
    sampler.cancel()
    await pool.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Browsers must revalidate the UI files, so a rebuilt image is never paired with a cached old app.js."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
async def index():
    # Version the asset URLs with their modification time as a second guard against stale caches.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        html = html.replace(f"/static/{name}", f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def get_config():
    return {"chunk_size": config.CHUNK_SIZE, "max_upload_bytes": config.MAX_UPLOAD_BYTES,
            "default_region": config.DEFAULT_REGION, "cores": CORES, "ram_mb": RAM_TOTAL_MB,
            "allow_reset": config.ALLOW_RESET, "cpus": len(ALLOWED_CPUS), "slots": config.IMPORT_CONCURRENCY}


@app.post("/api/reset")
async def reset():
    """Empty all import data and stored files so the same CSV can be imported again (POC only)."""
    if not config.ALLOW_RESET:
        raise HTTPException(403, "reset is disabled (ALLOW_RESET=0)")
    async with pool.connection() as conn:
        # Waits for a chunk that is mid-transaction; that worker then finds its import gone and stops.
        await conn.execute("TRUNCATE imports, contacts, import_errors, import_events RESTART IDENTITY")
    removed = 0
    for f in Path(config.DATA_DIR).glob("*.csv"):
        f.unlink(missing_ok=True)
        removed += 1
    return {"ok": True, "files_removed": removed}


async def event(conn, import_id, kind, data, at=None):
    await conn.execute("INSERT INTO import_events (import_id, kind, data, at) VALUES (%s, %s, %s, coalesce(%s, clock_timestamp()))",
                       (import_id, kind, json.dumps(data), at))


def read_header(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return next(csv.reader(f), [])


# A new import row; without a campaign it gets its own (campaign id = import id), like a separate customer.
NEW_IMPORT_SQL = """
WITH n AS (SELECT nextval(pg_get_serial_sequence('imports', 'id')) AS id)
INSERT INTO imports (id, campaign_id, file_name, cores) SELECT id, coalesce(%s, id), %s, %s FROM n
RETURNING id, campaign_id, created_at
"""


@app.post("/api/imports", status_code=202)
async def upload(request: Request, campaign_id: int | None = Query(None, ge=1),
                 cores: int | None = Query(None, ge=1, le=256)):
    """1. create import row  2. stream body to local storage + sha256  3. mark queued (= push to the queue).

    campaign_id: import into this campaign. Omitted: a new campaign of its own. Imports into different
    campaigns run in parallel; imports into the same campaign run one after another."""
    size_hint = int(request.headers.get("content-length") or 0)
    if size_hint > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    name = unquote(request.headers.get("x-file-name", "upload.csv"))[:255]

    async with pool.connection() as conn:
        cur = await conn.execute(NEW_IMPORT_SQL, (campaign_id, name, cores))
        row = await cur.fetchone()
        import_id, campaign_id = row["id"], row["campaign_id"]
        await event(conn, import_id, "created", {"file_name": name, "size": size_hint})

    path = Path(config.DATA_DIR) / f"{import_id}.csv"
    h, size, newlines, last = hashlib.sha256(), 0, 0, b"\n"
    usage = Usage(min_window=0.1)  # CPU and RAM of this API process while it receives the upload
    samples, pending, next_sample, t0 = [], [], 0, time.monotonic()

    def sample(digest, force=False):
        cores, rss = usage.tick(force)
        samples.append([round((time.monotonic() - t0) * 1000, 1), size, digest, cores, rss])
        pending.append(samples[-1])
        if usage.measured:  # the CPU figure covers every sample since the last measurement
            for s in pending:
                s[3] = cores
            pending.clear()
    try:
        with open(path, "wb") as f:
            async for piece in request.stream():         # receive next piece
                if not piece:
                    continue
                size += len(piece)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "file too large")
                f.write(piece)                            # write piece to storage
                h.update(piece)                           # update sha256
                newlines += piece.count(b"\n")
                last = piece[-1:]
                if size >= next_sample:
                    sample(h.copy().hexdigest()[:16])
                    next_sample = size + max(65536, size_hint * SAMPLE_EVERY)
        sample(h.hexdigest()[:16], force=True)
        if size == 0:
            raise HTTPException(400, "empty file")
        if not resolve(read_header(path))["phones"]:
            raise HTTPException(422, "no phone column: use 'phone' (or an Outlook export with Mobile/Business/Primary Phone)")
    except BaseException as e:
        path.unlink(missing_ok=True)
        async with pool.connection() as conn:
            await conn.execute("UPDATE imports SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                               (getattr(e, "detail", None) or "upload aborted", import_id))
        raise

    sha = h.hexdigest()
    est_rows = max(0, newlines + (0 if last == b"\n" else 1) - 1)
    try:
        # UNIQUE (campaign_id, file_sha256) decides which upload of the same file wins, even when two finish
        # at the same moment (a SELECT-then-UPDATE check would let both through).
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("""UPDATE imports SET file_path=%s, file_sha256=%s, file_size=%s, est_rows=%s,
                                  status='queued' WHERE id=%s""", (str(path), sha, size, est_rows, import_id))
            await event(conn, import_id, "uploaded", {"bytes": size, "sha256": sha, "est_rows": est_rows,
                                                       "path": str(path), "samples": samples})
            await event(conn, import_id, "queued", {"import_id": import_id})
    except UniqueViolation:  # same file already imported into this campaign: return that import
        path.unlink(missing_ok=True)
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute("SELECT id FROM imports WHERE campaign_id=%s AND file_sha256=%s", (campaign_id, sha))
            dup = await cur.fetchone()
            await conn.execute("DELETE FROM import_events WHERE import_id=%s", (import_id,))
            await conn.execute("DELETE FROM imports WHERE id=%s", (import_id,))
        return JSONResponse({"import_id": dup["id"], "duplicate": True}, status_code=200)
    return {"import_id": import_id, "duplicate": False}


RECENT_IMPORTS_SQL = """
SELECT id, campaign_id, file_name, status, cores, est_rows, file_size, error,
       checkpoint_row AS rows, valid_rows, invalid_rows, duplicate_rows,
       extract(epoch FROM created_at) * 1000 AS created_ms, extract(epoch FROM started_at) * 1000 AS started_ms,
       extract(epoch FROM finished_at) * 1000 AS finished_ms,
       round(extract(epoch FROM finished_at - started_at)::numeric, 2) AS seconds,
       round(extract(epoch FROM coalesce(finished_at, now()) - started_at)::numeric, 2) AS elapsed,
       (SELECT (data->>'cores_used')::int FROM import_events e
         WHERE e.import_id = i.id AND kind = 'opened' ORDER BY id DESC LIMIT 1) AS cores_used,
       -- queued behind another import of the same campaign (one import per campaign at a time)
       (SELECT o.id FROM imports o WHERE i.status = 'queued' AND o.campaign_id = i.campaign_id
          AND o.id <> i.id AND o.status = 'processing' LIMIT 1) AS blocked_by,
       (SELECT jsonb_build_object('chunks', count(*),
                                  'email_blanked', coalesce(sum((data->>'email_blanked')::int), 0),
                                  'date_blanked', coalesce(sum((data->>'date_blanked')::int), 0))
          FROM import_events e WHERE e.import_id = i.id AND kind = 'chunk') AS chunk_stats,
       (SELECT jsonb_object_agg(k, n) FROM (
          SELECT r.key AS k, sum(r.value::int) AS n FROM import_events e, jsonb_each_text(e.data->'reasons') r
          WHERE e.import_id = i.id AND e.kind = 'chunk' GROUP BY r.key) x) AS reasons
FROM imports i ORDER BY id DESC LIMIT %s
"""


@app.get("/api/imports")
async def list_imports(limit: int = Query(12, ge=1, le=100)):
    """Recent imports: progress, validation results, timing and whether one waits on its campaign."""
    async with pool.connection() as conn:
        cur = await conn.execute(RECENT_IMPORTS_SQL, (limit,))
        return await cur.fetchall()


@app.get("/api/monitor")
async def monitor(after: int = 0, window: int = Query(600, ge=10, le=1800), limit: int = Query(12, ge=1, le=100)):
    """Live system view, polled every second: CPU/memory samples from the api and worker (newer than `after`,
    at most `window` seconds old) plus the recent imports, so parallel runs line up with the load they caused."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, source, extract(epoch FROM at) * 1000 AS t, data FROM system_samples
               WHERE id > %s AND at > now() - make_interval(secs => %s) ORDER BY id""", (after, window))
        samples = await cur.fetchall()
        cur = await conn.execute(RECENT_IMPORTS_SQL, (limit,))
        imports = await cur.fetchall()
        cur = await conn.execute("SELECT extract(epoch FROM now()) * 1000 AS now")
        now = (await cur.fetchone())["now"]
    return {"now": now, "samples": samples, "imports": imports}


@app.post("/api/imports/{import_id}/rerun")
async def rerun(import_id: int, cores: int | None = Query(None, ge=1, le=256)):
    """Process an already uploaded file again, with a (different) core cap, into a fresh campaign so every
    run does identical work (no duplicates carried over from the previous run)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT * FROM imports WHERE id=%s", (import_id,))
        src = await cur.fetchone()
        if not src or not src["file_path"] or not Path(src["file_path"]).exists():
            raise HTTPException(404, "original file not found (it may have been reset)")
        cur = await conn.execute(
            """WITH n AS (SELECT nextval(pg_get_serial_sequence('imports', 'id')) AS id)
               INSERT INTO imports (id, campaign_id, file_name, file_path, file_sha256, file_size, est_rows, status, cores)
               SELECT id, id, %s, %s, %s, %s, %s, 'queued', %s FROM n
               RETURNING id, campaign_id""",
            (src["file_name"], src["file_path"], src["file_sha256"], src["file_size"], src["est_rows"], cores))
        row = await cur.fetchone()
        new_id = row["id"]
        await event(conn, new_id, "created", {"file_name": src["file_name"], "size": src["file_size"], "rerun_of": import_id})
        await event(conn, new_id, "uploaded", {"bytes": src["file_size"], "sha256": src["file_sha256"],
                                               "est_rows": src["est_rows"], "path": src["file_path"], "samples": [],
                                               "rerun_of": import_id})
        await event(conn, new_id, "queued", {"import_id": new_id})
    return {"import_id": new_id, "campaign_id": row["campaign_id"], "duplicate": False}


@app.get("/api/imports/{import_id}")
async def import_status(import_id: int, after_event: int = 0):
    """Polled by the UI: the import row (counters, never COUNT(*)) plus new timeline events."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM imports WHERE id=%s", (import_id,))
        imp = await cur.fetchone()
        if not imp:
            raise HTTPException(404, "import not found")
        cur = await conn.execute("SELECT id, kind, data, at FROM import_events WHERE import_id=%s AND id>%s ORDER BY id",
                                 (import_id, after_event))
        events = await cur.fetchall()
    return {"import": imp, "events": events}


@app.get("/api/campaigns/{campaign_id}/contacts")
async def contacts(campaign_id: int, after_id: int = 0, limit: int = Query(50, ge=1, le=500)):
    """Keyset pagination: WHERE id > cursor ORDER BY id. No OFFSET."""
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT id, phone_e164, name, vars, status FROM contacts
                                    WHERE campaign_id=%s AND id>%s ORDER BY id LIMIT %s""",
                                 (campaign_id, after_id, limit))
        return await cur.fetchall()
