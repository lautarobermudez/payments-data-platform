"""FastAPI application. Run it with:

    API_DATABASE_URL="postgresql://api_reader:api_reader@localhost:5432/payments" uvicorn api.main:app

Interactive documentation at /docs, machine-readable contract at /openapi.json.
"""
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import config
from api.db import Database
from api.routers import orders, payables, runs, suppliers
from api.schemas import Health

DESCRIPTION = """
Read-only API over the **payments data platform**: payment orders, suppliers, accounts payable and the ETL run log.

* Every endpoint is a `GET`. The database role it connects with cannot write, and cannot read bank account data.
* Amounts are numbers with two decimals, in the currency of the company.
* Order status values are the ones stored in the database: `pendiente` (pending), `pagada` (paid), `anulada` (cancelled).
* Lists are paginated with `limit` (1-200) and `offset`.
"""

TAGS = [
    {'name': 'orders', 'description': 'Payment orders with their lines, status history and the full change log.'},
    {'name': 'suppliers', 'description': 'Suppliers with pending and paid amounts, and supplier statements.'},
    {'name': 'payables', 'description': 'What is overdue or about to fall due, and the age of the pending amounts.'},
    {'name': 'runs', 'description': 'ETL executions and the rows that were quarantined.'},
    {'name': 'health'},
]


def create_app(dsn: str | None = None) -> FastAPI:
    db = Database(dsn or config.database_url())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.open()
        yield
        db.close()

    app = FastAPI(title='Payments Data Platform API', version='1.0.0', description=DESCRIPTION, openapi_tags=TAGS, lifespan=lifespan)
    app.state.db = db
    for module in (orders, suppliers, payables, runs):
        app.include_router(module.router)

    @app.get('/health', response_model=Health, tags=['health'], summary='Is the API up and can it reach the database?')
    def health():
        db.one('SELECT 1 AS ok')
        return Health(status='ok', database='up')

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(_request: Request, _exc: psycopg.OperationalError):
        return JSONResponse(status_code=503, content={'detail': 'The database is not available'})

    return app


app = create_app()
