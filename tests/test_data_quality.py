"""Run db/tests/data_quality.sql against a loaded database; every rule must report 0 violations.

    DATABASE_URL="dbname=payments" python -m unittest tests.test_data_quality
Skipped automatically when no database is configured.
"""
import os
import pathlib
import unittest

SQL = pathlib.Path(__file__).resolve().parents[1] / 'db' / 'tests' / 'data_quality.sql'


@unittest.skipUnless(os.environ.get('DATABASE_URL'), 'DATABASE_URL not set')
class DataQuality(unittest.TestCase):
    def test_all_rules_have_zero_violations(self):
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL']) as con:
            rows = con.execute(SQL.read_text()).fetchall()
        self.assertGreaterEqual(len(rows), 10)
        failing = [(rule, n) for rule, n in rows if n]
        self.assertEqual(failing, [], f'rules with violations: {failing}')

    def test_every_op_total_equals_sum_of_its_lines(self):
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL']) as con:
            bad = con.execute('SELECT count(*) FROM pagos.v_orden_pago v WHERE v.importe_total <> '
                              '(SELECT sum(importe) FROM pagos.orden_pago_detalle d WHERE d.orden_pago_id = v.orden_pago_id)'
                              ).fetchone()[0]
        self.assertEqual(bad, 0)


if __name__ == '__main__':
    unittest.main()
