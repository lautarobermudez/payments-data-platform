"""Payment orders: a filtered list and the full detail of one order with its history."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from api.db import like_pattern
from api.schemas import OrderDetail, OrderSummary, Page, Status

router = APIRouter(prefix='/orders', tags=['orders'])

SUMMARY = ('orden_pago_id AS id, empresa AS company, nro_op AS number, fecha_op AS order_date, proveedor AS supplier, '
           'forma_pago AS payment_method, estado AS status, fecha_pago AS paid_on, fecha_vencimiento AS due_on, '
           'importe_total AS total, renglones AS lines')
SORTS = {'order_date': 'fecha_op', '-order_date': 'fecha_op DESC', 'total': 'importe_total', '-total': 'importe_total DESC',
         'due_on': 'fecha_vencimiento NULLS LAST'}


@router.get('', response_model=Page[OrderSummary], summary='List payment orders')
def list_orders(
    request: Request,
    company: Annotated[str | None, Query(description='Exact company name')] = None,
    status: Annotated[Status | None, Query(description='pendiente (pending), pagada (paid) or anulada (cancelled)')] = None,
    supplier: Annotated[str | None, Query(description='Part of the supplier name, case-insensitive')] = None,
    date_from: Annotated[date | None, Query(description='Order date on or after')] = None,
    date_to: Annotated[date | None, Query(description='Order date on or before')] = None,
    due_before: Annotated[date | None, Query(description='Due date on or before')] = None,
    min_total: Annotated[float | None, Query(ge=0)] = None,
    sort: Literal['order_date', '-order_date', 'total', '-total', 'due_on'] = '-order_date',
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    where, params = [], {}
    if company:
        where.append('empresa = %(company)s')
        params['company'] = company
    if status:
        where.append('estado = %(status)s')
        params['status'] = status
    if supplier:
        where.append(r"proveedor ILIKE %(supplier)s ESCAPE '\'")
        params['supplier'] = like_pattern(supplier)
    if date_from:
        where.append('fecha_op >= %(date_from)s')
        params['date_from'] = date_from
    if date_to:
        where.append('fecha_op <= %(date_to)s')
        params['date_to'] = date_to
    if due_before:
        where.append('fecha_vencimiento <= %(due_before)s')
        params['due_before'] = due_before
    if min_total is not None:
        where.append('importe_total >= %(min_total)s')
        params['min_total'] = min_total
    rows, total = request.app.state.db.page(SUMMARY, 'pagos.v_orden_pago', where, params,
                                            f'{SORTS[sort]}, orden_pago_id', limit, offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.get('/{order_id}', response_model=OrderDetail, summary='One order with its lines, status history and change log')
def get_order(order_id: int, request: Request):
    db = request.app.state.db
    head = db.one(f'SELECT {SUMMARY}, condicion_detalle AS payment_condition, plazo AS term, referencia_pago AS payment_reference '
                  'FROM pagos.v_orden_pago WHERE orden_pago_id = %(id)s', {'id': order_id})
    if head is None:
        raise HTTPException(404, f'Order {order_id} not found')
    lines = db.all('SELECT renglon AS line_no, nro_comprobante AS invoice_number, concepto AS concept, '
                   'fecha_renglon AS line_date, importe AS amount FROM pagos.v_orden_pago_detalle '
                   'WHERE orden_pago_id = %(id)s ORDER BY renglon', {'id': order_id})
    history = db.all('SELECT s.nombre AS status, h.cambiado_en AS changed_at, h.nota AS note '
                     'FROM pagos.orden_pago_historial h JOIN pagos.estado_op s ON s.id = h.estado_id '
                     'WHERE h.orden_pago_id = %(id)s ORDER BY h.cambiado_en, h.id', {'id': order_id})
    log = db.all('SELECT id, changed_at, table_name, record_id, operation, changed_fields, old_row, new_row, actor, run_id '
                 'FROM audit.v_order_change_log WHERE orden_pago_id = %(id)s ORDER BY changed_at, id', {'id': order_id})
    return OrderDetail(**head, order_lines=lines, status_history=history, change_log=log)
