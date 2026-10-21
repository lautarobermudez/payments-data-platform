-- Query layer "ERP style" on top of schema pagos: query without writing JOINs.
-- Spend = pending + paid OPs (cancelled OPs are excluded unless the status is requested).
-- Amounts in local currency. Run after 01_schema.sql.

-- 1. The OP with its total (derived from lines).
CREATE VIEW pagos.v_orden_pago AS
SELECT o.id AS orden_pago_id, e.nombre AS empresa, o.nro_op, o.fecha_op,
       t.razon_social AS proveedor, f.nombre AS forma_pago, s.nombre AS estado,
       o.fecha_pago, o.fecha_vencimiento, o.condicion_detalle, o.plazo, o.referencia_pago,
       COALESCE(SUM(d.importe), 0)::numeric(14,2) AS importe_total,
       COUNT(d.id) AS renglones
FROM pagos.orden_pago o
JOIN pagos.empresa e    ON e.id = o.empresa_id
JOIN pagos.tercero t    ON t.id = o.tercero_id
JOIN pagos.forma_pago f ON f.id = o.forma_pago_id
JOIN pagos.estado_op s  ON s.id = o.estado_id
LEFT JOIN pagos.orden_pago_detalle d ON d.orden_pago_id = o.id
GROUP BY o.id, e.nombre, t.razon_social, f.nombre, s.nombre;

-- 2. Every line with the data of its OP (flat, for BI and for searching by concept or invoice).
CREATE VIEW pagos.v_orden_pago_detalle AS
SELECT o.orden_pago_id, o.empresa, o.nro_op, o.fecha_op, o.proveedor, o.estado,
       d.renglon, c.nro_comprobante, d.concepto, d.fecha AS fecha_renglon, d.importe
FROM pagos.orden_pago_detalle d
JOIN pagos.v_orden_pago o ON o.orden_pago_id = d.orden_pago_id
LEFT JOIN pagos.comprobante c ON c.id = d.comprobante_id;

-- 3. Supplier card: data, aliases, main account (the one of its latest OP) and activity.
CREATE VIEW pagos.v_proveedor AS
SELECT t.id AS tercero_id, t.razon_social, t.cuit, t.alias, t.es_cliente,
       b.nombre AS banco, c.cbu, c.nro_cuenta, c.titular,
       (SELECT count(*) FROM pagos.cuenta_bancaria x WHERE x.tercero_id = t.id) AS cuentas,
       (SELECT count(*) FROM pagos.orden_pago o WHERE o.tercero_id = t.id)      AS ops,
       (SELECT max(o.fecha_op) FROM pagos.orden_pago o WHERE o.tercero_id = t.id) AS ultima_op
FROM pagos.tercero t
LEFT JOIN LATERAL (SELECT o.cuenta_bancaria_id FROM pagos.orden_pago o
                   WHERE o.tercero_id = t.id AND o.cuenta_bancaria_id IS NOT NULL
                   ORDER BY o.fecha_op DESC, o.id DESC LIMIT 1) u ON true
LEFT JOIN pagos.cuenta_bancaria c ON c.id = u.cuenta_bancaria_id
LEFT JOIN pagos.banco b ON b.id = c.banco_id
WHERE t.es_proveedor;

-- 4. Supplier statement: its OPs in date order with the running pending balance.
CREATE VIEW pagos.v_estado_cuenta_proveedor AS
SELECT t.id AS tercero_id, t.razon_social AS proveedor, o.empresa, o.fecha_op, o.nro_op,
       o.estado, o.forma_pago, o.fecha_pago, o.fecha_vencimiento, o.importe_total,
       CASE WHEN o.estado = 'pendiente' THEN o.importe_total ELSE 0 END AS pendiente,
       SUM(CASE WHEN o.estado = 'pendiente' THEN o.importe_total ELSE 0 END)
           OVER (PARTITION BY t.id ORDER BY o.fecha_op, o.orden_pago_id) AS pendiente_acumulado
FROM pagos.v_orden_pago o
JOIN pagos.orden_pago x ON x.id = o.orden_pago_id
JOIN pagos.tercero t    ON t.id = x.tercero_id
WHERE o.estado <> 'anulada';

-- 5. Accounts payable: what is pending, with age and status against the due date.
CREATE VIEW pagos.v_cuentas_a_pagar AS
SELECT o.empresa, o.proveedor, o.nro_op, o.fecha_op, o.forma_pago, o.plazo,
       o.fecha_vencimiento, o.importe_total,
       (current_date - o.fecha_op) AS dias_desde_op,
       CASE WHEN o.fecha_vencimiento IS NULL        THEN 'sin vencimiento'
            WHEN o.fecha_vencimiento < current_date THEN 'vencida'
            WHEN o.fecha_vencimiento = current_date THEN 'vence hoy'
            ELSE 'a vencer' END AS situacion
FROM pagos.v_orden_pago o
WHERE o.estado = 'pendiente';

-- 6. Payments by month, company and payment method.
CREATE VIEW pagos.v_pagos_mensuales AS
SELECT date_trunc('month', fecha_op)::date AS mes, empresa, forma_pago,
       count(*) AS ops, sum(importe_total)::numeric(14,2) AS importe,
       sum(importe_total) FILTER (WHERE estado = 'pendiente')::numeric(14,2) AS pendiente
FROM pagos.v_orden_pago
WHERE estado <> 'anulada'
GROUP BY 1, 2, 3;

-- 7. Supplier ranking by company.
CREATE VIEW pagos.v_ranking_proveedores AS
SELECT empresa, proveedor, count(*) AS ops, sum(importe_total)::numeric(14,2) AS importe,
       round(avg(importe_total), 2) AS ticket_promedio, min(fecha_op) AS primera_op, max(fecha_op) AS ultima_op
FROM pagos.v_orden_pago
WHERE estado <> 'anulada'
GROUP BY empresa, proveedor;

-- 8. Invoices with the amount paid on each one and in how many OPs.
CREATE VIEW pagos.v_comprobante AS
SELECT c.id AS comprobante_id, t.razon_social AS tercero, c.sentido, c.nro_comprobante,
       count(DISTINCT d.orden_pago_id) AS ops, sum(d.importe)::numeric(14,2) AS imputado,
       min(o.fecha_op) AS primera_op, max(o.fecha_op) AS ultima_op,
       bool_and(s.nombre = 'pagada') AS todo_pagado
FROM pagos.comprobante c
JOIN pagos.tercero t ON t.id = c.tercero_id
JOIN pagos.orden_pago_detalle d ON d.comprobante_id = c.id
JOIN pagos.orden_pago o ON o.id = d.orden_pago_id
JOIN pagos.estado_op s ON s.id = o.estado_id
WHERE s.nombre <> 'anulada'
GROUP BY c.id, t.razon_social, c.sentido, c.nro_comprobante;

COMMENT ON VIEW pagos.v_orden_pago IS 'The OP with its total (sum of lines), company, supplier, payment method and status.';
COMMENT ON VIEW pagos.v_orden_pago_detalle IS 'Each line with the data of its OP and the invoice number.';
COMMENT ON VIEW pagos.v_proveedor IS 'Supplier card: data, aliases, main account (latest OP), number of accounts and OPs.';
COMMENT ON VIEW pagos.v_estado_cuenta_proveedor IS 'Supplier OPs in date order with the running pending amount.';
COMMENT ON VIEW pagos.v_cuentas_a_pagar IS 'Pending OPs with age and status against the due date (vencida, vence hoy, a vencer).';
COMMENT ON VIEW pagos.v_pagos_mensuales IS 'Count and amount of OPs by month, company and payment method (no cancelled).';
COMMENT ON VIEW pagos.v_ranking_proveedores IS 'Amount, count and average ticket by supplier and company (no cancelled).';
COMMENT ON VIEW pagos.v_comprobante IS 'Each invoice with the amount allocated in OPs, how many OPs pay it and whether it is fully paid.';
