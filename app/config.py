import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/csvimport")
DATA_DIR = os.getenv("DATA_DIR", "/data/imports")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "300")) * 1024 * 1024
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "10000"))
# Country used for numbers written without a country code when the row has no `country` value.
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "IN")
# Global cap on imports running at once (across all worker processes). Each slot is a thread in the worker;
# imports into different campaigns run in parallel, imports into the same campaign one after another.
IMPORT_CONCURRENCY = int(os.getenv("IMPORT_CONCURRENCY", "4"))
# POC convenience: the UI's "reset db" button empties every import table. Set to 0 anywhere real.
ALLOW_RESET = os.getenv("ALLOW_RESET", "1") == "1"
# Parallel validation slices per chunk when an import has no core cap; default cores - 1.
# (The pool itself starts one process per available CPU so any cap up to all cores can be used.)
# Counts the CPUs this process may use (a container or `taskset` can allow fewer than the host has).
_AVAILABLE_CPUS = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 2)
POOL_SIZE = int(os.getenv("POOL_SIZE", "0")) or max(1, _AVAILABLE_CPUS - 1)
LOCK_TIMEOUT = os.getenv("LOCK_TIMEOUT", "10 min")
