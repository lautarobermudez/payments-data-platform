"""Pure validation helpers for Argentine tax ids (CUIT) and bank account codes (CBU).

No database access here, so they are easy to unit test and reuse in the generator and the loader.
"""
import re
import unicodedata

_CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_CBU_BLOCK1_WEIGHTS = (7, 1, 3, 9, 7, 1, 3)
_CBU_BLOCK2_WEIGHTS = (3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3)


def digits(value) -> str:
    """Keep only the digits of a value ('30-0000-1234-5' -> '3000001234 5' without spaces)."""
    return re.sub(r'\D', '', str(value or ''))


def cuit_check_digit(first_ten: str) -> int:
    total = sum(int(d) * w for d, w in zip(first_ten, _CUIT_WEIGHTS))
    result = 11 - total % 11
    return {11: 0, 10: 9}.get(result, result)


def is_valid_cuit(value) -> bool:
    d = digits(value)
    return len(d) == 11 and cuit_check_digit(d[:10]) == int(d[10])


def _cbu_check(block: str, weights) -> int:
    total = sum(int(d) * w for d, w in zip(block, weights))
    return (10 - total % 10) % 10


def is_valid_cbu(value) -> bool:
    d = digits(value)
    return (len(d) == 22
            and _cbu_check(d[:7], _CBU_BLOCK1_WEIGHTS) == int(d[7])
            and _cbu_check(d[8:21], _CBU_BLOCK2_WEIGHTS) == int(d[21]))


def make_cbu(bank_code: str, branch: str, account: str) -> str:
    """Build a structurally valid CBU from a 3-digit bank, 4-digit branch and 13-digit account."""
    block1 = bank_code + branch
    block2 = account
    return block1 + str(_cbu_check(block1, _CBU_BLOCK1_WEIGHTS)) + block2 + str(_cbu_check(block2, _CBU_BLOCK2_WEIGHTS))


def normalize_name(name: str) -> str:
    """Upper case, no accents, no punctuation, single spaces: the key used to match supplier names."""
    s = unicodedata.normalize('NFKD', str(name or '')).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9 ]+', ' ', s).upper()
    return re.sub(r'\s+', ' ', s).strip()


# ---------------------------------------------------------------------------
# Row-level rules for the incremental loader (pure: no database).
# ---------------------------------------------------------------------------
import datetime as _dt  # noqa: E402
from decimal import Decimal, InvalidOperation  # noqa: E402


def is_iso_date(value) -> bool:
    try:
        _dt.date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def order_problems(order: dict, lines: list, *, company_known: bool, supplier_known: bool,
                   methods, statuses) -> list:
    """Every reason why a payment order (header + lines) must be quarantined; an empty list means it is valid.

    `order` and `lines` are the CSV rows as dictionaries. The caller says whether the company and the supplier
    were found, because that depends on the database.
    """
    problems = []
    if not company_known:
        problems.append('unknown company')
    if order.get('forma_pago') not in methods:
        problems.append(f'unknown payment method {order.get("forma_pago")!r}')
    if order.get('estado') not in statuses:
        problems.append(f'unknown status {order.get("estado")!r}')
    if not supplier_known:
        problems.append('supplier not loaded')
    if not lines:
        problems.append('OP without lines')
    for field in ('fecha_op', 'fecha_pago', 'fecha_vencimiento'):
        if order.get(field) and not is_iso_date(order[field]):
            problems.append(f'invalid date in {field}')
    if not order.get('fecha_op'):
        problems.append('missing fecha_op')
    if order.get('estado') != 'pagada' and order.get('fecha_pago'):
        problems.append(f'{order.get("estado")} OP with a payment date')
    if order.get('estado') == 'pagada' and not order.get('fecha_pago'):
        problems.append('paid OP without a payment date')
    seen = set()
    for line in lines:
        try:
            amount = Decimal(str(line.get('importe')))
        except InvalidOperation:
            problems.append(f'invalid line amount {line.get("importe")!r}')
            continue
        if amount == 0:
            problems.append('line with a zero amount')
        number = line.get('renglon')
        if not str(number).isdigit() or int(number) < 1:
            problems.append(f'invalid line number {number!r}')
        elif number in seen:
            problems.append(f'duplicate line number {number}')
        seen.add(number)
    return problems
