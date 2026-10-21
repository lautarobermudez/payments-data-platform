-- Paid amount per month with one column per company and a total (pivot with FILTER), in millions.
SELECT to_char(date_trunc('month', fecha_op), 'YYYY-MM') AS month,
       round(sum(importe_total) FILTER (WHERE empresa = 'Alpha Trading S.A.')     / 1e6, 1) AS alpha_mm,
       round(sum(importe_total) FILTER (WHERE empresa = 'Beta Foods S.A.')        / 1e6, 1) AS beta_mm,
       round(sum(importe_total) FILTER (WHERE empresa = 'Gamma Logistics S.R.L.') / 1e6, 1) AS gamma_mm,
       round(sum(importe_total) / 1e6, 1)                                                   AS total_mm
FROM pagos.v_orden_pago
WHERE estado = 'pagada'
GROUP BY date_trunc('month', fecha_op)
ORDER BY 1;
