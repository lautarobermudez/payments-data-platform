"""The ETL run log: what was loaded, when, with what outcome, and the quarantined rows of each run."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from api.schemas import Page, RejectedRow, Run

router = APIRouter(prefix='/runs', tags=['runs'])

SELECT = ('r.run_id, r.mode, r.status, r.source, r.source_hash, r.started_at, r.finished_at, '
          'extract(epoch FROM r.finished_at - r.started_at)::float AS duration_seconds, r.counts, r.error, '
          '(SELECT count(*) FROM audit.v_rejected_row x WHERE x.run_id = r.run_id) AS rejected_rows')


@router.get('', response_model=Page[Run], summary='Recent ETL runs, newest first')
def list_runs(
    request: Request,
    status: Literal['running', 'succeeded', 'succeeded_with_rejects', 'failed'] | None = None,
    mode: Literal['dry-run', 'apply'] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    where, params = [], {}
    if status:
        where.append('r.status = %(status)s')
        params['status'] = status
    if mode:
        where.append('r.mode = %(mode)s')
        params['mode'] = mode
    rows, total = request.app.state.db.page(SELECT, 'audit.load_run r', where, params, 'r.started_at DESC, r.run_id', limit, offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.get('/{run_id}', response_model=Run, summary='One ETL run')
def get_run(run_id: UUID, request: Request):
    row = request.app.state.db.one(f'SELECT {SELECT} FROM audit.load_run r WHERE r.run_id = %(id)s', {'id': run_id})
    if row is None:
        raise HTTPException(404, f'Run {run_id} not found')
    return row


@router.get('/{run_id}/rejects', response_model=Page[RejectedRow], summary='Rows quarantined by one run (bank details removed)')
def run_rejects(
    run_id: UUID, request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    db = request.app.state.db
    if db.one('SELECT 1 AS ok FROM audit.load_run WHERE run_id = %(id)s', {'id': run_id}) is None:
        raise HTTPException(404, f'Run {run_id} not found')
    rows, total = db.page('id, entity, source_file, line_no, natural_key AS key, reason, raw, rejected_at', 'audit.v_rejected_row',
                          ['run_id = %(id)s'], {'id': run_id}, 'id', limit, offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)
