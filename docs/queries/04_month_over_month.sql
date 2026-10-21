-- Total paid per month and change against the previous month (LAG window function).
WITH monthly AS (
    SELECT date_trunc('month', fecha_op) AS month, sum(importe_total) AS total
    FROM pagos.v_orden_pago
    WHERE estado = 'pagada'
    GROUP BY 1
)
SELECT to_char(month, 'YYYY-MM') AS month,
       total,
       round(100 * (total - lag(total) OVER (ORDER BY month)) / lag(total) OVER (ORDER BY month), 1) AS change_pct
FROM monthly
ORDER BY month;
