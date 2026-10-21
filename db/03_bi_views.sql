-- Schema "bi": flat, stable views meant for BI tools (Tableau, Power BI). They hide the model:
-- a dashboard connects here and never to the pagos tables. Bank details are deliberately left out.
-- Spend = pending + paid OPs; cancelled OPs are kept so the dashboard can filter on estado.
CREATE SCHEMA IF NOT EXISTS bi;

CREATE VIEW bi.v_ops AS
SELECT o.orden_pago_id                          AS op_id,
       o.empresa,
       o.nro_op,
       o.fecha_op,
       date_trunc('month', o.fecha_op)::date    AS mes,
       extract(year FROM o.fecha_op)::int       AS anio,
       o.proveedor,
       o.estado,
       o.forma_pago,
       o.condicion_detalle                      AS condicion_pago,
       o.importe_total,
       o.fecha_pago,
       CASE WHEN o.estado = 'pendiente'
            THEN current_date - o.fecha_op END  AS dias_pendiente,
       o.fecha_vencimiento,
       o.renglones
FROM pagos.v_orden_pago o;

CREATE VIEW bi.v_pendientes AS
SELECT empresa, proveedor, count(*) AS cant_ops, sum(importe_total) AS total_pendiente,
       min(fecha_op) AS op_mas_vieja, max(dias_pendiente) AS dias_max_antiguedad
FROM bi.v_ops
WHERE estado = 'pendiente'
GROUP BY empresa, proveedor;

CREATE VIEW bi.v_gasto_mensual AS
SELECT empresa, mes, estado, count(*) AS cant_ops, sum(importe_total) AS total
FROM bi.v_ops
WHERE estado <> 'anulada'
GROUP BY empresa, mes, estado;

CREATE VIEW bi.v_proveedores AS
SELECT empresa, proveedor, count(*) AS cant_ops, sum(importe_total) AS total,
       min(fecha_op) AS primera_op, max(fecha_op) AS ultima_op,
       rank() OVER (PARTITION BY empresa ORDER BY sum(importe_total) DESC) AS ranking
FROM bi.v_ops
WHERE estado <> 'anulada'
GROUP BY empresa, proveedor;

-- Lines with their cost centre ('Sin asignar' until classified).
CREATE VIEW bi.v_ops_centro_costo AS
SELECT o.orden_pago_id, o.empresa, COALESCE(c.nombre, 'Sin asignar') AS centro_costo,
       o.nro_op, o.fecha_op, date_trunc('month', o.fecha_op)::date AS mes,
       o.proveedor, o.estado, d.renglon, d.concepto, d.importe
FROM pagos.orden_pago_detalle d
JOIN pagos.v_orden_pago o ON o.orden_pago_id = d.orden_pago_id
LEFT JOIN pagos.centro_costo c ON c.id = d.centro_costo_id;

COMMENT ON SCHEMA bi IS 'Stable, flat views for BI tools.';
