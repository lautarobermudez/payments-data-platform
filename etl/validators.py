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
