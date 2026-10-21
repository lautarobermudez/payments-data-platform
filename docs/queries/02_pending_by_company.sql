-- Pending OPs by company: count, total, oldest OP and age in days.
SELECT empresa,
       count(*)                       AS ops,
       sum(importe_total)             AS pending_total,
       min(fecha_op)                  AS oldest_op,
       current_date - min(fecha_op)   AS age_days
FROM pagos.v_orden_pago
WHERE estado = 'pendiente'
GROUP BY empresa
ORDER BY pending_total DESC;
