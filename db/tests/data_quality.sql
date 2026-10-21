-- Data-quality rules for schema pagos: each query returns the number of rows that BREAK the rule.
-- Every value must be 0. Run through tests/test_data_quality.py (or psql -f).
SELECT 'paid date only on paid OPs' AS regla, count(*) AS violaciones
FROM pagos.orden_pago o JOIN pagos.estado_op s ON s.id = o.estado_id
WHERE o.fecha_pago IS NOT NULL AND s.nombre <> 'pagada'
UNION ALL
SELECT 'every OP has at least one line', count(*)
FROM pagos.orden_pago o WHERE NOT EXISTS (SELECT 1 FROM pagos.orden_pago_detalle d WHERE d.orden_pago_id = o.id)
UNION ALL
SELECT 'every OP has a status history', count(*)
FROM pagos.orden_pago o WHERE NOT EXISTS (SELECT 1 FROM pagos.orden_pago_historial h WHERE h.orden_pago_id = o.id)
UNION ALL
SELECT 'last history status = OP status', count(*)
FROM pagos.orden_pago o
WHERE o.estado_id <> (SELECT h.estado_id FROM pagos.orden_pago_historial h
                      WHERE h.orden_pago_id = o.id ORDER BY h.cambiado_en DESC, h.id DESC LIMIT 1)
UNION ALL
SELECT 'bank of the CBU = bank of the account', count(*)
FROM pagos.cuenta_bancaria c JOIN pagos.banco b ON b.id = c.banco_id
WHERE c.cbu IS NOT NULL AND left(c.cbu, 3) <> b.codigo
UNION ALL
SELECT 'account of the OP belongs to the same supplier', count(*)
FROM pagos.orden_pago o JOIN pagos.cuenta_bancaria c ON c.id = o.cuenta_bancaria_id
WHERE c.tercero_id <> o.tercero_id
UNION ALL
SELECT 'invoice of a line belongs to the third party of the OP', count(*)
FROM pagos.orden_pago_detalle d JOIN pagos.orden_pago o ON o.id = d.orden_pago_id
JOIN pagos.comprobante f ON f.id = d.comprobante_id WHERE f.tercero_id <> o.tercero_id
UNION ALL
SELECT 'alias repeated in more than one supplier', count(*)
FROM (SELECT a FROM pagos.tercero, unnest(alias) AS a GROUP BY a HAVING count(DISTINCT id) > 1) x
UNION ALL
SELECT 'alias equal to the supplier own tax id or name', count(*)
FROM pagos.tercero p, unnest(p.alias) AS a WHERE a = p.cuit OR upper(a) = upper(p.razon_social)
UNION ALL
SELECT 'every OP is paid to a third party with the supplier role', count(*)
FROM pagos.orden_pago o JOIN pagos.tercero t ON t.id = o.tercero_id WHERE NOT t.es_proveedor
UNION ALL
SELECT 'OP number repeated inside a company', count(*)
FROM (SELECT empresa_id, nro_op FROM pagos.orden_pago WHERE nro_op IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 1) x
UNION ALL
SELECT 'cost centre belongs to the company of the OP', count(*)
FROM pagos.orden_pago_detalle d
JOIN pagos.orden_pago o ON o.id = d.orden_pago_id
JOIN pagos.centro_costo c ON c.id = d.centro_costo_id
WHERE c.empresa_id <> o.empresa_id
UNION ALL
SELECT 'treasury: running balance consistent with movements', count(*)
FROM (SELECT cuenta_id, orden, saldo,
             saldo - lag(saldo) OVER (PARTITION BY cuenta_id ORDER BY orden) AS delta, importe
      FROM tesoreria.movimientos) m
WHERE m.delta IS NOT NULL AND m.delta <> m.importe;
