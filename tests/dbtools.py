"""Helpers for the tests that need a real PostgreSQL: a throw-away database per test, built from the repo's own SQL.

Each test gets a fresh database (schema, views, audit, baseline load), so tests never depend on a shared state or on the
order they run in. Needs DATABASE_URL pointing to a server where the user may CREATE DATABASE (true for the Docker and CI
databases of this project); the tests are skipped otherwise.
"""
import contextlib
import os
import pathlib
import subprocess
import sys
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT_FILES = ['db/01_schema.sql', 'db/02_views.sql', 'db/03_bi_views.sql', 'db/04_treasury.sql', 'db/05_audit.sql',
              'db/06_api_role.sql']
LOADER = ROOT / 'etl' / 'load_seed.py'
BASE_URL = os.environ.get('DATABASE_URL')


def _dsn_for(dbname):
    info = conninfo_to_dict(BASE_URL)
    info['dbname'] = dbname
    return make_conninfo(**info)


@contextlib.contextmanager
def temp_database(load_baseline=True):
    """Yield the DSN of a new database with the full schema (and, by default, the baseline seed already loaded)."""
    name = f'test_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE {name}')
    dsn = _dsn_for(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as con:
            for f in INIT_FILES:
                con.execute((ROOT / f).read_text())
        if load_baseline:
            result = load(dsn, apply=True)
            assert result.returncode == 0, result.stdout + result.stderr
        yield dsn
    finally:
        with psycopg.connect(BASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def load(dsn, source=None, apply=False, *extra):
    """Run the loader as a user would, from the command line, and return the finished process."""
    cmd = [sys.executable, str(LOADER), '--dsn', dsn]
    if source:
        cmd += ['--source', str(source)]
    if apply:
        cmd.append('--apply')
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def scalar(dsn, sql, params=None):
    with psycopg.connect(dsn) as con:
        return con.execute(sql, params).fetchone()[0]


def rows(dsn, sql, params=None):
    with psycopg.connect(dsn) as con:
        return con.execute(sql, params).fetchall()
