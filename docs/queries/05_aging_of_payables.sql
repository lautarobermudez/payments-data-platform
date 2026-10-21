-- Accounts payable aging: pending amount by company and due-date bucket (uses view v_cuentas_a_pagar).
SELECT empresa,
       sum(importe_total) FILTER (WHERE situacion = 'vencida')         AS overdue,
       sum(importe_total) FILTER (WHERE situacion = 'vence hoy')       AS due_today,
       sum(importe_total) FILTER (WHERE situacion = 'a vencer')        AS upcoming,
       sum(importe_total) FILTER (WHERE situacion = 'sin vencimiento') AS no_due_date,
       sum(importe_total)                                              AS total_pending
FROM pagos.v_cuentas_a_pagar
GROUP BY empresa
ORDER BY total_pending DESC;
