"""HTTP API + static UI.  Run: uvicorn app.main:app

Expected CSV schema (header names, case-insensitive):
  phone    required
  name     optional
  country  optional, used for numbers written without a country code
  anything else is kept in contacts.vars
"""
import csv
import hashlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from . import config
from .db import init_schema

STATIC = Path(__file__).parent / "static"
SAMPLE_EVERY = 1 / 60  # record ~60 upload progress samples for the timeline
pool: AsyncConnectionPool = None  # opened in lifespan


@asynccontextmanager
async def lifespan(_app):
    global pool
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    init_schema()
    pool = AsyncConnectionPool(config.DATABASE_URL, min_size=1, max_size=10, open=False,
                               kwargs={"autocommit": True, "row_factory": dict_row})
    await pool.open()
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config():
    return {"chunk_size": config.CHUNK_SIZE, "max_upload_bytes": config.MAX_UPLOAD_BYTES,
            "default_region": config.DEFAULT_REGION}


async def event(conn, import_id, kind, data, at=None):
    await conn.execute("INSERT INTO import_events (import_id, kind, data, at) VALUES (%s, %s, %s, coalesce(%s, clock_timestamp()))",
                       (import_id, kind, json.dumps(data), at))


def read_header(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return [h.strip().lower() for h in next(csv.reader(f), [])]


@app.post("/api/imports", status_code=202)
async def upload(request: Request, campaign_id: int = 1):
    """1. create import row  2. stream body to local storage + sha256  3. mark queued (= push to the queue)."""
    size_hint = int(request.headers.get("content-length") or 0)
    if size_hint > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    name = unquote(request.headers.get("x-file-name", "upload.csv"))[:255]

    async with pool.connection() as conn:
        cur = await conn.execute("INSERT INTO imports (campaign_id, file_name) VALUES (%s, %s) RETURNING id, created_at",
                                 (campaign_id, name))
        row = await cur.fetchone()
        import_id = row["id"]
        await event(conn, import_id, "created", {"file_name": name, "size": size_hint})

    path = Path(config.DATA_DIR) / f"{import_id}.csv"
    h, size, newlines, last = hashlib.sha256(), 0, 0, b"\n"
    samples, next_sample, t0 = [], 0, time.monotonic()
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
                    samples.append([round((time.monotonic() - t0) * 1000, 1), size, h.copy().hexdigest()[:16]])
                    next_sample = size + max(65536, size_hint * SAMPLE_EVERY)
        samples.append([round((time.monotonic() - t0) * 1000, 1), size, h.hexdigest()[:16]])
        if size == 0:
            raise HTTPException(400, "empty file")
        if "phone" not in read_header(path):
            raise HTTPException(422, "CSV must have a 'phone' column (optional: name, country)")
    except BaseException as e:
        path.unlink(missing_ok=True)
        async with pool.connection() as conn:
            await conn.execute("UPDATE imports SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                               (getattr(e, "detail", None) or "upload aborted", import_id))
        raise

    sha = h.hexdigest()
    est_rows = max(0, newlines + (0 if last == b"\n" else 1) - 1)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT id FROM imports WHERE campaign_id=%s AND file_sha256=%s", (campaign_id, sha))
        dup = await cur.fetchone()
        if dup:  # same file already imported into this campaign
            path.unlink(missing_ok=True)
            await conn.execute("DELETE FROM import_events WHERE import_id=%s", (import_id,))
            await conn.execute("DELETE FROM imports WHERE id=%s", (import_id,))
            return JSONResponse({"import_id": dup["id"], "duplicate": True}, status_code=200)
        await conn.execute("""UPDATE imports SET file_path=%s, file_sha256=%s, file_size=%s, est_rows=%s,
                              status='queued' WHERE id=%s""", (str(path), sha, size, est_rows, import_id))
        await event(conn, import_id, "uploaded", {"bytes": size, "sha256": sha, "est_rows": est_rows,
                                                   "path": str(path), "samples": samples})
        await event(conn, import_id, "queued", {"import_id": import_id})
    return {"import_id": import_id, "duplicate": False}


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
