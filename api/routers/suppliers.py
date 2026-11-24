"""Suppliers with their activity, and the statement of one supplier (its orders with the running pending balance)."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from api.db import like_pattern
from api.schemas import Page, StatementRow, Supplier

router = APIRouter(prefix='/suppliers', tags=['suppliers'])

SOURCE = ('pagos.tercero t LEFT JOIN pagos.orden_pago o ON o.tercero_id = t.id '
          'LEFT JOIN pagos.estado_op s ON s.id = o.estado_id LEFT JOIN pagos.v_orden_pago v ON v.orden_pago_id = o.id')
SELECT = ("t.id, t.razon_social AS name, t.cuit AS tax_id, t.alias AS aliases, "
          "count(o.id) FILTER (WHERE s.nombre <> 'anulada') AS orders, "
          "COALESCE(sum(v.importe_total) FILTER (WHERE s.nombre = 'pendiente'), 0) AS pending_amount, "
          "COALESCE(sum(v.importe_total) FILTER (WHERE s.nombre = 'pagada'), 0) AS paid_amount, "
          "max(o.fecha_op) FILTER (WHERE s.nombre <> 'anulada') AS last_order_date")


@router.get('', response_model=Page[Supplier], summary='List suppliers with pending and paid amounts')
def list_suppliers(
    request: Request,
    search: Annotated[str | None, Query(description='Part of the name or the tax id')] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    db = request.app.state.db
    where, params = ['t.es_proveedor'], {}
    if search:
        where.append(r"(t.razon_social ILIKE %(search)s ESCAPE '\' OR t.cuit LIKE %(search)s ESCAPE '\')")
        params['search'] = like_pattern(search)
    clause = ' AND '.join(where)
    total = db.one(f'SELECT count(*) AS n FROM pagos.tercero t WHERE {clause}', params)['n']
    rows = db.all(f'SELECT {SELECT} FROM {SOURCE} WHERE {clause} GROUP BY t.id ORDER BY t.razon_social, t.id '
                  'LIMIT %(limit)s OFFSET %(offset)s', {**params, 'limit': limit, 'offset': offset})
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.get('/{supplier_id}/statement', response_model=list[StatementRow], summary='Statement of one supplier')
def supplier_statement(supplier_id: int, request: Request):
    db = request.app.state.db
    if db.one('SELECT 1 AS ok FROM pagos.tercero WHERE id = %(id)s AND es_proveedor', {'id': supplier_id}) is None:
        raise HTTPException(404, f'Supplier {supplier_id} not found')
    return db.all('SELECT empresa AS company, fecha_op AS order_date, nro_op AS number, estado AS status, '
                  'forma_pago AS payment_method, fecha_pago AS paid_on, fecha_vencimiento AS due_on, importe_total AS total, '
                  'pendiente AS pending, pendiente_acumulado AS running_pending '
                  'FROM pagos.v_estado_cuenta_proveedor WHERE tercero_id = %(id)s ORDER BY fecha_op, nro_op NULLS LAST',
                  {'id': supplier_id})
