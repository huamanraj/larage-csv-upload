CREATE TABLE IF NOT EXISTS imports (
  id bigserial PRIMARY KEY,
  campaign_id bigint NOT NULL,
  file_path text NOT NULL,
  file_name text,
  file_sha256 text NOT NULL,
  file_size bigint NOT NULL DEFAULT 0,
  est_rows int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'uploaded', -- uploaded|queued|processing|done|failed
  mapping jsonb,
  default_region text NOT NULL DEFAULT 'IN',
  checkpoint_row int NOT NULL DEFAULT 0,
  valid_rows int DEFAULT 0, invalid_rows int DEFAULT 0, duplicate_rows int DEFAULT 0,
  reject_reasons jsonb NOT NULL DEFAULT '{}',
  worker_id text,
  locked_at timestamptz, error text,
  created_at timestamptz DEFAULT now(), started_at timestamptz, finished_at timestamptz,
  UNIQUE (campaign_id, file_sha256)
);
CREATE INDEX IF NOT EXISTS imports_queue_idx ON imports (status, created_at);

CREATE TABLE IF NOT EXISTS contacts (
  id bigserial PRIMARY KEY,
  campaign_id bigint NOT NULL,
  import_id bigint NOT NULL,
  phone_e164 text NOT NULL,
  name text,
  vars jsonb,
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz DEFAULT now(),
  UNIQUE (campaign_id, phone_e164)
);
CREATE INDEX IF NOT EXISTS contacts_campaign_status_id_idx ON contacts (campaign_id, status, id);
CREATE INDEX IF NOT EXISTS contacts_import_id_idx ON contacts (import_id, id);

CREATE TABLE IF NOT EXISTS import_errors (
  import_id bigint, row_no int, raw_phone text, reason text,
  PRIMARY KEY (import_id, row_no)
);

-- Written in the same transaction as each chunk, so the UI timeline never runs ahead of the data.
CREATE TABLE IF NOT EXISTS import_events (
  id bigserial PRIMARY KEY,
  import_id bigint NOT NULL,
  kind text NOT NULL,
  data jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS import_events_import_idx ON import_events (import_id, id);
