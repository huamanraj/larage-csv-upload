import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/csvimport")
DATA_DIR = os.getenv("DATA_DIR", "/data/imports")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "300")) * 1024 * 1024
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "10000"))
PREVIEW_ROWS = int(os.getenv("PREVIEW_ROWS", "50"))
# Global cap on imports running at once (across all worker processes).
IMPORT_CONCURRENCY = int(os.getenv("IMPORT_CONCURRENCY", "1"))
# Validation processes; default cores - 1.
POOL_SIZE = int(os.getenv("POOL_SIZE", "0")) or max(1, (os.cpu_count() or 2) - 1)
LOCK_TIMEOUT = os.getenv("LOCK_TIMEOUT", "10 min")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
ANALYZE_AFTER_ROWS = int(os.getenv("ANALYZE_AFTER_ROWS", "100000"))
# Artificial pause between chunks, only useful to slow a demo down.
CHUNK_DELAY_MS = int(os.getenv("CHUNK_DELAY_MS", "0"))
