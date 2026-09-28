"""HTTP API + static UI.  Run: uvicorn app.main:app"""
import csv
import hashlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from . import config
from .db import init_schema
from .phones import REGIONS, score_columns, suggest_country, suggest_name

STATIC = Path(__file__).parent / "static"
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
    return {"chunk_size": config.CHUNK_SIZE, "pool_size": config.POOL_SIZE, "max_upload_bytes": config.MAX_UPLOAD_BYTES,
            "concurrency": config.IMPORT_CONCURRENCY, "preview_rows": config.PREVIEW_ROWS,
            "regions": REGIONS}


# ---------------------------------------------------------------- 1. upload

@app.post("/api/imports", status_code=202)
async def upload(request: Request, campaign_id: int = 1):
    """Raw body upload. Streamed to disk with sha256 computed on the fly; never parsed or held in RAM."""
    declared = request.headers.get("content-length")
    if declared and int(declared) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    name = unquote(request.headers.get("x-file-name", "upload.csv"))[:255]
    tmp = Path(config.DATA_DIR) / f".upload-{uuid.uuid4().hex}.part"
    h, size, newlines, last = hashlib.sha256(), 0, 0, b"\n"
    t0 = time.monotonic()
    try:
        with open(tmp, "wb") as f:
            async for part in request.stream():
                if not part:
                    continue
                size += len(part)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "file too large")
                h.update(part)
                newlines += part.count(b"\n")
                last = part[-1:]
                f.write(part)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "empty file")
    sha = h.hexdigest()
    # Row estimate for the progress UI (header excluded; quoted multi-line cells make it approximate).
    est_rows = max(0, newlines + (0 if last == b"\n" else 1) - 1)

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """INSERT INTO imports (campaign_id, file_path, file_name, file_sha256, file_size, est_rows)
                   VALUES (%s, '', %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, file_sha256) DO NOTHING RETURNING id""",
                (campaign_id, name, sha, size, est_rows))
            row = await cur.fetchone()
            if row is None:  # same file already uploaded to this campaign: idempotent
                tmp.unlink(missing_ok=True)
                cur = await conn.execute("SELECT id, status FROM imports WHERE campaign_id=%s AND file_sha256=%s",
                                         (campaign_id, sha))
                existing = await cur.fetchone()
                return JSONResponse({"import_id": existing["id"], "status": existing["status"], "duplicate": True},
                                    status_code=200)
            import_id = row["id"]
            final = Path(config.DATA_DIR) / f"{import_id}.csv"
            os.replace(tmp, final)
            await conn.execute("UPDATE imports SET file_path=%s WHERE id=%s", (str(final), import_id))
            await conn.execute(
                "INSERT INTO import_events (import_id, kind, data) VALUES (%s, 'uploaded', %s)",
                (import_id, json.dumps({"bytes": size, "sha256": sha, "est_rows": est_rows,
                                        "seconds": round(time.monotonic() - t0, 3)})))
    return {"import_id": import_id, "status": "uploaded", "duplicate": False, "sha256": sha,
            "bytes": size, "est_rows": est_rows}


# ---------------------------------------------------------------- 2. preview + mapping

async def get_import(conn, import_id):
    cur = await conn.execute("SELECT * FROM imports WHERE id=%s", (import_id,))
    imp = await cur.fetchone()
    if not imp:
        raise HTTPException(404, "import not found")
    return imp


def read_head(path, n):
    """Read only the header and the first n rows."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        headers = next(reader, None) or []
        rows = []
        for rec in reader:
            if rec:
                rows.append(rec)
            if len(rows) >= n:
                break
    return headers, rows


@app.get("/api/imports/{import_id}/preview")
async def preview(import_id: int, country_column: str | None = None, region: str | None = None):
    """Scores use the country column (suggested one unless given) so foreign numbers aren't misread."""
    async with pool.connection() as conn:
        imp = await get_import(conn, import_id)
    if not Path(imp["file_path"]).exists():
        raise HTTPException(410, "source file no longer on disk")
    headers, rows = read_head(imp["file_path"], config.PREVIEW_ROWS)
    if not headers:
        raise HTTPException(422, "file has no header row")
    suggested_country = suggest_country(headers)
    country = suggested_country if country_column is None else (country_column or None)
    country_idx = headers.index(country) if country in headers else None
    scores = score_columns(headers, rows, (region or imp["default_region"]).upper(), country_idx)
    ranked = sorted(scores, key=lambda s: s["score"], reverse=True)
    phones = [s["header"] for s in ranked if s["valid_pct"] >= 0.2 and (s["name_score"] > 0 or s["valid_pct"] >= 0.6)][:3]
    return {"headers": headers, "rows": rows, "columns": scores,
            "suggested": {"phone_columns": phones, "name_column": suggest_name(headers), "var_columns": [],
                          "country_column": suggested_country, "infer_country_code": True},
            "mapping": imp["mapping"], "default_region": imp["default_region"]}


class Mapping(BaseModel):
    phone_columns: list[str] = Field(min_length=1)
    name_column: str | None = None
    var_columns: list[str] = []
    default_region: str = Field("IN", min_length=2, max_length=2)
    reject_landline: bool = False
    country_column: str | None = None   # per-row country for numbers written without a country code
    infer_country_code: bool = True     # retry 11+ digit numbers as if the '+' was dropped


@app.post("/api/imports/{import_id}/mapping")
async def set_mapping(import_id: int, m: Mapping):
    if m.default_region.upper() not in {code for code, _ in REGIONS}:
        raise HTTPException(422, f"unknown region {m.default_region}")
    async with pool.connection() as conn:
        imp = await get_import(conn, import_id)
        headers, _ = read_head(imp["file_path"], 0)
        missing = [c for c in [*m.phone_columns, *m.var_columns, *([m.name_column] if m.name_column else []),
                                *([m.country_column] if m.country_column else [])]
                   if c not in headers]
        if missing:
            raise HTTPException(422, f"unknown columns: {missing}")
        mapping = m.model_dump(exclude={"default_region"})
        async with conn.transaction():
            cur = await conn.execute(
                """UPDATE imports SET mapping=%s, default_region=%s, status='queued'
                   WHERE id=%s AND (status='uploaded' OR (status='failed' AND checkpoint_row=0)) RETURNING id""",
                (json.dumps(mapping), m.default_region.upper(), import_id))
            if not await cur.fetchone():
                raise HTTPException(409, f"import is {imp['status']}; mapping is locked")
            await conn.execute("INSERT INTO import_events (import_id, kind, data) VALUES (%s, 'queued', %s)",
                               (import_id, json.dumps({**mapping, "default_region": m.default_region.upper()})))
    return {"import_id": import_id, "status": "queued"}


@app.post("/api/imports/{import_id}/retry")
async def retry(import_id: int):
    """Re-queue a failed import. Its checkpoint is kept, so it resumes where it stopped."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("UPDATE imports SET status='queued', error=NULL, finished_at=NULL "
                                 "WHERE id=%s AND status='failed' AND mapping IS NOT NULL RETURNING checkpoint_row",
                                 (import_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(409, "only failed imports can be retried")
        await conn.execute("INSERT INTO import_events (import_id, kind, data) VALUES (%s, 'queued', %s)",
                           (import_id, json.dumps({"retry": True, "checkpoint_row": row["checkpoint_row"]})))
    return {"import_id": import_id, "status": "queued"}


# ---------------------------------------------------------------- status + fetch

@app.get("/api/imports")
async def list_imports(campaign_id: int | None = None, limit: int = Query(20, le=100)):
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, campaign_id, file_name, file_size, est_rows, status, checkpoint_row,
                      valid_rows, invalid_rows, duplicate_rows, created_at, finished_at
               FROM imports WHERE (%(c)s::bigint IS NULL OR campaign_id=%(c)s)
               ORDER BY id DESC LIMIT %(l)s""", {"c": campaign_id, "l": limit})
        return await cur.fetchall()


@app.get("/api/imports/{import_id}")
async def import_status(import_id: int, after_event: int = 0):
    """Polled by the UI. Counters come from the imports row, never COUNT(*) over contacts."""
    async with pool.connection() as conn:
        imp = await get_import(conn, import_id)
        cur = await conn.execute("SELECT id, kind, data, at FROM import_events WHERE import_id=%s AND id>%s "
                                 "ORDER BY id LIMIT 2000", (import_id, after_event))
        events = await cur.fetchall()
        queue = None
        if imp["status"] == "queued":
            cur = await conn.execute("SELECT count(*) AS ahead FROM imports WHERE status='queued' AND created_at < %s",
                                     (imp["created_at"],))
            ahead = (await cur.fetchone())["ahead"]
            cur = await conn.execute(f"SELECT count(*) AS running FROM imports WHERE status='processing' "
                                     f"AND locked_at >= now() - interval '{config.LOCK_TIMEOUT}'")
            queue = {"ahead": ahead, "running": (await cur.fetchone())["running"], "slots": config.IMPORT_CONCURRENCY}
    imp.pop("file_path", None)
    return {"import": imp, "events": events, "queue": queue}


@app.get("/api/campaigns/{campaign_id}/contacts")
async def contacts(campaign_id: int, status: str | None = None, after_id: int = 0,
                   before_id: int | None = None, limit: int = Query(50, ge=1, le=500)):
    """Keyset pagination on (campaign_id, status, id). No OFFSET."""
    async with pool.connection() as conn:
        if before_id is not None:
            cur = await conn.execute(
                """SELECT * FROM (SELECT id, phone_e164, name, vars, status, import_id FROM contacts
                   WHERE campaign_id=%s AND (%s::text IS NULL OR status=%s) AND id < %s
                   ORDER BY id DESC LIMIT %s) t ORDER BY id""",
                (campaign_id, status, status, before_id, limit))
        else:
            cur = await conn.execute(
                """SELECT id, phone_e164, name, vars, status, import_id FROM contacts
                   WHERE campaign_id=%s AND (%s::text IS NULL OR status=%s) AND id > %s
                   ORDER BY id LIMIT %s""",
                (campaign_id, status, status, after_id, limit))
        rows = await cur.fetchall()
    return {"items": rows, "first_id": rows[0]["id"] if rows else None, "last_id": rows[-1]["id"] if rows else None}


@app.get("/api/imports/{import_id}/errors")
async def errors(import_id: int, after_row: int = 0, before_row: int | None = None,
                 limit: int = Query(50, ge=1, le=500)):
    async with pool.connection() as conn:
        if before_row is not None:
            cur = await conn.execute(
                """SELECT * FROM (SELECT row_no, raw_phone, reason FROM import_errors
                   WHERE import_id=%s AND row_no < %s ORDER BY row_no DESC LIMIT %s) t ORDER BY row_no""",
                (import_id, before_row, limit))
        else:
            cur = await conn.execute("SELECT row_no, raw_phone, reason FROM import_errors "
                                     "WHERE import_id=%s AND row_no > %s ORDER BY row_no LIMIT %s",
                                     (import_id, after_row, limit))
        rows = await cur.fetchall()
    return {"items": rows, "first_row": rows[0]["row_no"] if rows else None,
            "last_row": rows[-1]["row_no"] if rows else None}


# ---------------------------------------------------------------- exports (COPY TO STDOUT, streamed)

def stream_copy(query: sql.Composable, filename: str):
    async def gen():
        async with pool.connection() as conn:
            async with conn.cursor().copy(
                    sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER)").format(query)) as copy:
                async for data in copy:
                    yield bytes(data)
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/campaigns/{campaign_id}/contacts.csv")
async def export_contacts(campaign_id: int, status: str | None = None):
    q = sql.SQL("SELECT id, phone_e164, name, vars, status, import_id FROM contacts WHERE campaign_id = {}").format(
        sql.Literal(campaign_id))
    if status:
        q = sql.SQL("{} AND status = {}").format(q, sql.Literal(status))
    return stream_copy(sql.SQL("{} ORDER BY id").format(q), f"campaign-{campaign_id}-contacts.csv")


@app.get("/api/imports/{import_id}/errors.csv")
async def export_errors(import_id: int):
    q = sql.SQL("SELECT row_no, raw_phone, reason FROM import_errors WHERE import_id = {} ORDER BY row_no").format(
        sql.Literal(import_id))
    return stream_copy(q, f"import-{import_id}-rejected.csv")
