-- Schema "tesoreria" (treasury): bank movements (payments, collections, income) of the companies.
-- The load is idempotent: a movement is identified by (account, date, debit, credit, occurrence),
-- where "occurrence" numbers identical movements of the same day, so reloading a statement never
-- duplicates rows while legitimately repeated movements are kept.
CREATE SCHEMA IF NOT EXISTS tesoreria;

CREATE TABLE tesoreria.cuentas (
    id            smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    empresa_id    smallint NOT NULL REFERENCES pagos.empresa(id),
    banco         text     NOT NULL,
    nombre        text     NOT NULL,
    tipo          text     NOT NULL CHECK (tipo IN ('cuenta corriente', 'cuenta corriente especial', 'caja de ahorro')),
    numero        text,
    fecha_cierre  date,                       -- NULL = open or unknown
    UNIQUE (empresa_id, banco, nombre)
);

CREATE TABLE tesoreria.movimientos (
    id          bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cuenta_id   smallint NOT NULL REFERENCES tesoreria.cuentas(id),
    fecha       date    NOT NULL,
    orden       integer NOT NULL,             -- position inside the account (keeps the bank order within a day)
    descripcion text    NOT NULL DEFAULT '',
    detalle     text    NOT NULL DEFAULT '',
    referencia  text    NOT NULL DEFAULT '',
    debito      numeric(14,2) NOT NULL DEFAULT 0 CHECK (debito  >= 0),
    credito     numeric(14,2) NOT NULL DEFAULT 0 CHECK (credito >= 0),
    importe     numeric(14,2) GENERATED ALWAYS AS (credito - debito) STORED,   -- + in, - out
    saldo       numeric(14,2),                -- balance after the movement (may be null)
    ocurrencia  smallint NOT NULL DEFAULT 1,
    creado_en   timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT (debito > 0 AND credito > 0)),
    UNIQUE (cuenta_id, fecha, debito, credito, ocurrencia)
);
CREATE INDEX idx_mov_cuenta_orden ON tesoreria.movimientos (cuenta_id, orden);
CREATE INDEX idx_mov_fecha        ON tesoreria.movimientos (fecha);

-- BI views. Income = credits, expenses = debits. They include transfers between own accounts;
-- separating them needs categorisation (a later stage).
CREATE VIEW bi.v_movimientos AS
SELECT m.id, e.nombre AS empresa, c.banco, c.nombre AS cuenta, m.fecha,
       date_trunc('month', m.fecha)::date AS mes,
       m.descripcion, m.detalle, m.referencia, m.debito, m.credito, m.importe, m.saldo
FROM tesoreria.movimientos m
JOIN tesoreria.cuentas c ON c.id = m.cuenta_id
JOIN pagos.empresa   e ON e.id = c.empresa_id;

CREATE VIEW bi.v_resumen_mensual AS
WITH ultimo AS (
    SELECT DISTINCT ON (cuenta_id, date_trunc('month', fecha))
           cuenta_id, date_trunc('month', fecha)::date AS mes, saldo
    FROM tesoreria.movimientos
    ORDER BY cuenta_id, date_trunc('month', fecha), orden DESC
)
SELECT e.nombre AS empresa, c.banco, c.nombre AS cuenta,
       date_trunc('month', m.fecha)::date AS mes,
       count(*) AS movimientos, sum(m.credito) AS ingresos, sum(m.debito) AS egresos,
       sum(m.importe) AS neto, u.saldo AS saldo_cierre
FROM tesoreria.movimientos m
JOIN tesoreria.cuentas c ON c.id = m.cuenta_id
JOIN pagos.empresa   e ON e.id = c.empresa_id
JOIN ultimo u ON u.cuenta_id = m.cuenta_id AND u.mes = date_trunc('month', m.fecha)::date
GROUP BY e.nombre, c.banco, c.nombre, date_trunc('month', m.fecha)::date, u.saldo;

CREATE VIEW bi.v_flujo_mensual AS
SELECT empresa, mes, sum(movimientos) AS movimientos, sum(ingresos) AS ingresos,
       sum(egresos) AS egresos, sum(neto) AS neto
FROM bi.v_resumen_mensual
GROUP BY empresa, mes;
