-- Top 3 suppliers per company (paid OPs): amount, share of the company and cumulative share.
-- Shows: CTEs, window functions (rank, partitioned sums), filtering on a window result.
WITH paid AS (                        -- 1) paid total by company and supplier
    SELECT empresa, proveedor, count(*) AS ops, sum(importe_total) AS total
    FROM pagos.v_orden_pago
    WHERE estado = 'pagada'
    GROUP BY empresa, proveedor
),
ranked AS (                           -- 2) position, % and running % inside each company
    SELECT empresa, proveedor, ops, total,
           rank() OVER (PARTITION BY empresa ORDER BY total DESC)         AS position,
           round(100 * total / sum(total) OVER (PARTITION BY empresa), 1) AS pct_company,
           round(100 * sum(total) OVER (PARTITION BY empresa ORDER BY total DESC)
                     / sum(total) OVER (PARTITION BY empresa), 1)         AS pct_cumulative
    FROM paid
)
SELECT empresa, position, proveedor, ops, total, pct_company, pct_cumulative
FROM ranked
WHERE position <= 3                   -- 3) keep the first three of each company
ORDER BY empresa, position;
