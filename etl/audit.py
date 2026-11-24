"""Bookkeeping for the loader: run log, row hashing, change classification and quarantine.

Everything here is about *traceability*; the business loading lives in load_seed.py.

  * RunLog writes audit.load_run on its OWN autocommit connection. The loader works inside one transaction that is rolled
    back on a dry-run or on a failure, and the record of that run must survive the rollback: a failed run is exactly the
    one you want to find later.
  * row_hash / classify implement the incremental load: a source row whose hash did not change is skipped.
"""
import hashlib
import json
import pathlib
from dataclasses import dataclass, field

import psycopg


def canonical(obj) -> str:
    """Stable JSON: same content -> same text, whatever the key order."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def row_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()


def files_hash(directory) -> str:
    """One hash for a whole input folder (names and bytes of its CSV files, in name order)."""
    h = hashlib.sha256()
    for path in sorted(pathlib.Path(directory).glob('*.csv')):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def classify(stored_hash, new_hash) -> str:
    """'new' (never loaded), 'unchanged' (same content) or 'changed' (loaded before with other content)."""
    if stored_hash is None:
        return 'new'
    return 'unchanged' if stored_hash == new_hash else 'changed'


@dataclass
class Rejection:
    entity: str
    key: str
    reason: str
    source_file: str = ''
    line_no: int = 0
    raw: dict = field(default_factory=dict)


class RunLog:
    """One row in audit.load_run, opened when the load starts and closed (succeeded / failed) when it ends."""

    def __init__(self, dsn, mode, source, source_hash):
        self.con = psycopg.connect(dsn, autocommit=True)
        self.run_id = self.con.execute(
            'INSERT INTO audit.load_run (mode, source, source_hash) VALUES (%s, %s, %s) RETURNING run_id',
            (mode, source, source_hash)).fetchone()[0]

    def finish(self, status, counts, error=None, rejections=()):
        """Close the run. Quarantined rows are stored even for a dry-run or a failed run, so the reasons can be read later."""
        with self.con.transaction():
            for r in rejections:
                self.con.execute(
                    'INSERT INTO audit.rejected_row (run_id, entity, source_file, line_no, natural_key, reason, raw) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                    (self.run_id, r.entity, r.source_file, r.line_no, r.key, r.reason, json.dumps(r.raw, default=str)))
            self.con.execute(
                'UPDATE audit.load_run SET finished_at = now(), status = %s, counts = %s, error = %s WHERE run_id = %s',
                (status, json.dumps(counts), error, self.run_id))
        self.con.close()
