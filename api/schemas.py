"""Response models. They define the contract that /docs and /openapi.json publish."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, PlainSerializer

T = TypeVar('T')

# Amounts are exact decimals inside; in JSON they travel as plain numbers with two decimals.
Money = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used='json')]
Status = Literal['pendiente', 'pagada', 'anulada']      # pending / paid / cancelled (values stored in the database)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class OrderSummary(BaseModel):
    id: int
    company: str
    number: int | None
    order_date: date
    supplier: str
    payment_method: str
    status: str
    paid_on: date | None
    due_on: date | None
    total: Money
    lines: int


class OrderLine(BaseModel):
    line_no: int
    invoice_number: str | None
    concept: str | None
    line_date: date | None
    amount: Money


class StatusChange(BaseModel):
    status: str
    changed_at: datetime
    note: str | None


class ChangeLogEntry(BaseModel):
    id: int
    changed_at: datetime
    table_name: str
    record_id: int
    operation: Literal['INSERT', 'UPDATE', 'DELETE']
    changed_fields: list[str] | None
    old_row: dict | None
    new_row: dict | None
    actor: str
    run_id: UUID | None


class OrderDetail(OrderSummary):
    payment_condition: str | None
    term: str | None
    payment_reference: str | None
    order_lines: list[OrderLine]
    status_history: list[StatusChange]
    change_log: list[ChangeLogEntry]


class Supplier(BaseModel):
    id: int
    name: str
    tax_id: str | None
    aliases: list[str]
    orders: int
    pending_amount: Money
    paid_amount: Money
    last_order_date: date | None


class StatementRow(BaseModel):
    company: str
    order_date: date
    number: int | None
    status: str
    payment_method: str
    paid_on: date | None
    due_on: date | None
    total: Money
    pending: Money
    running_pending: Money


class DueItem(BaseModel):
    id: int
    company: str
    number: int | None
    supplier: str
    order_date: date
    due_on: date
    days_overdue: int          # negative = days left until the due date
    total: Money
    payment_method: str


class AgingRow(BaseModel):
    company: str
    not_due: Money             # due date today or later
    overdue_1_30: Money
    overdue_31_60: Money
    overdue_61_90: Money
    overdue_over_90: Money
    no_due_date: Money
    total: Money


class Aging(BaseModel):
    as_of: date
    rows: list[AgingRow]
    totals: AgingRow


class Run(BaseModel):
    run_id: UUID
    mode: Literal['dry-run', 'apply']
    status: Literal['running', 'succeeded', 'succeeded_with_rejects', 'failed']
    source: str
    source_hash: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    counts: dict[str, int]
    error: str | None
    rejected_rows: int


class RejectedRow(BaseModel):
    id: int
    entity: str
    source_file: str
    line_no: int | None
    key: str | None
    reason: str
    raw: dict
    rejected_at: datetime


class Health(BaseModel):
    status: Literal['ok']
    database: Literal['up']
