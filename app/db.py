from pathlib import Path

import psycopg

from . import config

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def init_schema() -> None:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        # Serialize concurrent startups (api + worker) running the DDL.
        conn.execute("SELECT pg_advisory_lock(7331)")
        try:
            conn.execute(SCHEMA)
        finally:
            conn.execute("SELECT pg_advisory_unlock(7331)")
