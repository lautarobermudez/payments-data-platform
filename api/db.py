"""A small wrapper over a psycopg connection pool. Every query is read-only twice over: the database role cannot write,
and each connection is opened with default_transaction_read_only."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, dsn: str):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=5, open=False,
                                   kwargs={'row_factory': dict_row, 'options': '-c default_transaction_read_only=on'})

    def open(self):
        self.pool.open(wait=True, timeout=10)

    def close(self):
        self.pool.close()

    def all(self, sql: str, params: dict | None = None) -> list:
        with self.pool.connection() as con:
            return con.execute(sql, params or {}).fetchall()

    def one(self, sql: str, params: dict | None = None):
        with self.pool.connection() as con:
            return con.execute(sql, params or {}).fetchone()

    def page(self, select: str, source: str, where: list, params: dict, order_by: str, limit: int, offset: int):
        """(rows, total) for a filtered, sorted and paginated list. `select`, `source` and `order_by` are fixed strings
        written in this code base (never user input); the filters travel as bound parameters."""
        clause = f'WHERE {" AND ".join(where)}' if where else ''
        total = self.one(f'SELECT count(*) AS n FROM {source} {clause}', params)['n']
        rows = self.all(f'SELECT {select} FROM {source} {clause} ORDER BY {order_by} LIMIT %(limit)s OFFSET %(offset)s',
                        {**params, 'limit': limit, 'offset': offset})
        return rows, total


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards in a search term and wrap it for a 'contains' match."""
    escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'
