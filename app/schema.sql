-- One row per uploaded file. With status='queued' it is also the job queue entry.
CREATE TABLE IF NOT EXISTS imports (
  id bigserial PRIMARY KEY,
  campaign_id bigint NOT NULL,
  file_name text,
  file_path text,
  file_sha256 text,                          -- known once the upload finishes
  file_size bigint NOT NULL DEFAULT 0,
  est_rows int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'uploading',  -- uploading|queued|processing|done|failed
  checkpoint_row int NOT NULL DEFAULT 0,
  valid_rows int NOT NULL DEFAULT 0,
  invalid_rows int NOT NULL DEFAULT 0,
  duplicate_rows int NOT NULL DEFAULT 0,
  cores int,                                 -- CPU cap for this import (NULL = auto)
  worker_id text,
  locked_at timestamptz,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (campaign_id, file_sha256)          -- same file twice = same import
);
-- Upgrade tables created by earlier versions (CREATE TABLE IF NOT EXISTS never alters them).
-- The row is now created before the file exists, so path and hash start out empty.
ALTER TABLE imports ALTER COLUMN file_path DROP NOT NULL;
ALTER TABLE imports ALTER COLUMN file_sha256 DROP NOT NULL;
ALTER TABLE imports ALTER COLUMN status SET DEFAULT 'uploading';
ALTER TABLE imports ADD COLUMN IF NOT EXISTS file_name text;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS file_size bigint NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS est_rows int NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS worker_id text;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS cores int;
CREATE INDEX IF NOT EXISTS imports_queue_idx ON imports (status, created_at);

CREATE TABLE IF NOT EXISTS contacts (
  id bigserial PRIMARY KEY,
  campaign_id bigint NOT NULL,
  import_id bigint NOT NULL,
  phone_e164 text NOT NULL,
  name text,
  vars jsonb,                                -- every other column of the row
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, phone_e164)           -- dedupe is enforced by the DB
);
CREATE INDEX IF NOT EXISTS contacts_campaign_status_id_idx ON contacts (campaign_id, status, id);

CREATE TABLE IF NOT EXISTS import_errors (
  import_id bigint, row_no int, raw_phone text, reason text,
  PRIMARY KEY (import_id, row_no)
);

-- Timeline for the UI. Chunk events are written in the same transaction as the chunk's data.
CREATE TABLE IF NOT EXISTS import_events (
  id bigserial PRIMARY KEY,
  import_id bigint NOT NULL,
  kind text NOT NULL,
  data jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS import_events_import_idx ON import_events (import_id, id);
