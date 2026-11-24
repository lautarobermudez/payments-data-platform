"""Accounts payable: what falls due soon and how old the pending amounts are. Both take an `as_of` date so a result can be
reproduced for any day (default: today)."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request

from api.schemas import Aging, AgingRow, DueItem, Page

router = APIRouter(prefix='/payables', tags=['payables'])

MONEY_BUCKETS = ('not_due', 'overdue_1_30', 'overdue_31_60', 'overdue_61_90', 'overdue_over_90', 'no_due_date', 'total')


@router.get('/due', response_model=Page[DueItem], summary='Pending orders overdue or due within the next N days')
def payables_due(
    request: Request,
    as_of: Annotated[date | None, Query(description='Reference day (default: today)')] = None,
    days: Annotated[int, Query(ge=0, le=365, description='Look-ahead window in days')] = 30,
    company: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    where = ["estado = 'pendiente'", 'fecha_vencimiento IS NOT NULL', 'fecha_vencimiento <= %(as_of)s::date + %(days)s']
    params = {'as_of': as_of or date.today(), 'days': days}
    if company:
        where.append('empresa = %(company)s')
        params['company'] = company
    select = ('orden_pago_id AS id, empresa AS company, nro_op AS number, proveedor AS supplier, fecha_op AS order_date, '
              'fecha_vencimiento AS due_on, (%(as_of)s::date - fecha_vencimiento) AS days_overdue, importe_total AS total, '
              'forma_pago AS payment_method')
    rows, total = request.app.state.db.page(select, 'pagos.v_orden_pago', where, params, 'fecha_vencimiento, orden_pago_id',
                                            limit, offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.get('/aging', response_model=Aging, summary='Pending amount by company and age against the due date')
def payables_aging(
    request: Request,
    as_of: Annotated[date | None, Query(description='Reference day (default: today)')] = None,
    company: str | None = None,
):
    day = as_of or date.today()
    params = {'as_of': day}
    extra = ''
    if company:
        extra, params['company'] = ' AND empresa = %(company)s', company
    late = '(%(as_of)s::date - fecha_vencimiento)'
    rows = request.app.state.db.all(
        'SELECT empresa AS company, '
        'COALESCE(sum(importe_total) FILTER (WHERE fecha_vencimiento >= %(as_of)s::date), 0) AS not_due, '
        f'COALESCE(sum(importe_total) FILTER (WHERE {late} BETWEEN 1 AND 30), 0) AS overdue_1_30, '
        f'COALESCE(sum(importe_total) FILTER (WHERE {late} BETWEEN 31 AND 60), 0) AS overdue_31_60, '
        f'COALESCE(sum(importe_total) FILTER (WHERE {late} BETWEEN 61 AND 90), 0) AS overdue_61_90, '
        f'COALESCE(sum(importe_total) FILTER (WHERE {late} > 90), 0) AS overdue_over_90, '
        'COALESCE(sum(importe_total) FILTER (WHERE fecha_vencimiento IS NULL), 0) AS no_due_date, '
        'COALESCE(sum(importe_total), 0) AS total '
        f"FROM pagos.v_orden_pago WHERE estado = 'pendiente'{extra} GROUP BY empresa ORDER BY total DESC, empresa", params)
    totals = {'company': 'TOTAL', **{k: sum(r[k] for r in rows) for k in MONEY_BUCKETS}}
    return Aging(as_of=day, rows=[AgingRow(**r) for r in rows], totals=AgingRow(**totals))
