-- 06_api_role.sql: the database role the REST API connects with, and the audit views it may read.
-- Safe to run more than once. Run after 05_audit.sql.
--
-- Least privilege, enforced by PostgreSQL and not by the application:
--   * the role can only read (default_transaction_read_only, plus SELECT-only grants);
--   * it cannot read bank accounts: no grant on pagos.cuenta_bancaria, and no access to pagos.v_proveedor (which shows the CBU);
--   * it cannot read audit.change_log directly, because that table also holds the history of pagos.cuenta_bancaria
--     (JSON that contains the CBU); it reads two views that leave the sensitive parts out;
--   * every query is cut after 5 seconds.

-- The API only needs these two slices of the audit trail.
CREATE OR REPLACE VIEW audit.v_order_change_log AS
SELECT c.id, c.changed_at, c.table_name, c.record_id, c.operation, c.changed_fields, c.old_row, c.new_row, c.actor, c.run_id,
       (COALESCE(c.new_row, c.old_row) ->> CASE c.table_name WHEN 'pagos.orden_pago' THEN 'id' ELSE 'orden_pago_id' END)::bigint
           AS orden_pago_id
FROM audit.change_log c
WHERE c.table_name IN ('pagos.orden_pago', 'pagos.orden_pago_detalle');

CREATE OR REPLACE VIEW audit.v_rejected_row AS
SELECT id, run_id, entity, source_file, line_no, natural_key, reason, raw - 'cbu' AS raw, rejected_at
FROM audit.rejected_row;

COMMENT ON VIEW audit.v_order_change_log IS 'Change history of payment orders and their lines only (no bank data); what the API serves.';
COMMENT ON VIEW audit.v_rejected_row IS 'Quarantined rows without the bank account number; what the API serves.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'api_reader') THEN
        -- Local demo credentials. In a real deployment the password comes from a secret store.
        CREATE ROLE api_reader LOGIN PASSWORD 'api_reader' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

ALTER ROLE api_reader SET default_transaction_read_only = on;
ALTER ROLE api_reader SET statement_timeout = '5s';

REVOKE ALL ON ALL TABLES IN SCHEMA pagos, audit FROM api_reader;
GRANT USAGE ON SCHEMA pagos, audit TO api_reader;
GRANT SELECT ON pagos.v_orden_pago, pagos.v_orden_pago_detalle, pagos.v_estado_cuenta_proveedor,
                pagos.orden_pago, pagos.orden_pago_historial, pagos.estado_op, pagos.empresa TO api_reader;
GRANT SELECT (id, razon_social, cuit, alias, es_proveedor) ON pagos.tercero TO api_reader;   -- no bank data lives here
GRANT SELECT ON audit.load_run, audit.v_order_change_log, audit.v_rejected_row TO api_reader;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO api_reader', current_database());
END $$;
