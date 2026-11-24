-- 05_audit.sql: change history, load-run log, incremental-load bookkeeping and quarantine.
-- Safe to run more than once (every statement is idempotent).
--
-- What lives here and why:
--   audit.change_log   who changed what and when, written by a trigger so that NOTHING escapes (loader, API or a manual psql edit).
--   audit.load_run     one row per ETL execution (dry-runs and failures included); written on its own connection.
--   audit.source_row   content hash of every source row already loaded, so a load only touches what changed.
--   audit.rejected_row quarantine: invalid rows with the reason and the raw values; the rest of the load goes on.

SET client_min_messages = warning;   -- DROP ... IF EXISTS would print a notice on a first run

CREATE SCHEMA IF NOT EXISTS audit;

-- ---------- Execution log ----------
CREATE TABLE IF NOT EXISTS audit.load_run (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    mode         text NOT NULL CHECK (mode IN ('dry-run', 'apply')),
    source       text NOT NULL,                       -- directory that was loaded
    source_hash  text NOT NULL,                       -- sha256 over the input files (same hash = same input)
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'succeeded_with_rejects', 'failed')),
    counts       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- new / changed / unchanged / rejected per entity
    error        text
);

-- ---------- Change history (append-only) ----------
CREATE TABLE IF NOT EXISTS audit.change_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changed_at     timestamptz NOT NULL DEFAULT now(),
    table_name     text   NOT NULL,                   -- e.g. pagos.orden_pago
    record_id      bigint NOT NULL,                   -- id of the changed row
    operation      text   NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    changed_fields text[],                            -- only for UPDATE: the columns whose value changed
    old_row        jsonb,
    new_row        jsonb,
    actor          text   NOT NULL,                   -- app.actor if the caller set it, else the database user
    run_id         uuid REFERENCES audit.load_run (run_id)   -- the ETL run that made the change, if any
);
CREATE INDEX IF NOT EXISTS idx_change_log_record ON audit.change_log (table_name, record_id, changed_at);
CREATE INDEX IF NOT EXISTS idx_change_log_run ON audit.change_log (run_id);

CREATE OR REPLACE FUNCTION audit.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.% is append-only', TG_TABLE_NAME;
END $$;

DROP TRIGGER IF EXISTS trg_append_only ON audit.change_log;
CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON audit.change_log
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();

-- One generic trigger function for every audited table.
-- An UPDATE that changes nothing (ignoring the bookkeeping timestamps) writes no row, so re-running a load
-- never pollutes the history.
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ignored constant text[] := ARRAY['creado_en', 'actualizado_en'];
    old_j   jsonb;
    new_j   jsonb;
    changed text[];
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN old_j := to_jsonb(OLD) - ignored; END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN new_j := to_jsonb(NEW) - ignored; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF old_j = new_j THEN
            RETURN NULL;
        END IF;
        SELECT array_agg(n.key ORDER BY n.key) INTO changed
        FROM jsonb_each(new_j) AS n
        WHERE old_j -> n.key IS DISTINCT FROM n.value;
    END IF;
    INSERT INTO audit.change_log (table_name, record_id, operation, changed_fields, old_row, new_row, actor, run_id)
    VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
            (COALESCE(new_j, old_j) ->> 'id')::bigint,
            TG_OP, changed, old_j, new_j,
            COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user),
            NULLIF(current_setting('app.run_id', true), '')::uuid);
    RETURN NULL;
END $$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['orden_pago', 'orden_pago_detalle', 'tercero', 'comprobante', 'cuenta_bancaria'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_audit ON pagos.%I', t);
        EXECUTE format('CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON pagos.%I '
                       'FOR EACH ROW EXECUTE FUNCTION audit.log_change()', t);
    END LOOP;
END $$;

-- ---------- Incremental load: what was loaded from the source ----------
CREATE TABLE IF NOT EXISTS audit.source_row (
    entity       text NOT NULL,                       -- 'order'
    natural_key  text NOT NULL,                       -- e.g. 'Alpha Trading S.A.|17'
    row_hash     text NOT NULL,                       -- sha256 of the canonical content (order + its lines)
    first_run_id uuid NOT NULL REFERENCES audit.load_run (run_id),
    last_run_id  uuid NOT NULL REFERENCES audit.load_run (run_id),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity, natural_key)
);

-- ---------- Quarantine ----------
CREATE TABLE IF NOT EXISTS audit.rejected_row (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES audit.load_run (run_id),
    entity      text NOT NULL,                        -- supplier / bank account / order
    source_file text NOT NULL,
    line_no     integer,                              -- line in the source file (header = 1)
    natural_key text,
    reason      text NOT NULL,
    raw         jsonb NOT NULL,                       -- the row exactly as received
    rejected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rejected_run ON audit.rejected_row (run_id);

-- ---------- Documentation inside the database ----------
COMMENT ON SCHEMA audit IS 'Traceability: change history, ETL run log, incremental-load hashes and quarantined rows.';
COMMENT ON TABLE audit.load_run IS 'One row per ETL execution, including dry-runs and failed runs (written on its own connection so it survives a rollback).';
COMMENT ON TABLE audit.change_log IS 'Append-only history of every INSERT/UPDATE/DELETE on the audited pagos tables, written by trigger.';
COMMENT ON TABLE audit.source_row IS 'Hash of each source row already loaded; an unchanged hash means the row is skipped.';
COMMENT ON TABLE audit.rejected_row IS 'Rows that failed validation, with the reason and the raw values. The rest of the load is not blocked.';
COMMENT ON COLUMN audit.change_log.actor IS 'Set by the caller with SET LOCAL app.actor; falls back to the database user.';

RESET client_min_messages;
