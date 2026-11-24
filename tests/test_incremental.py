"""Integration tests for the incremental load and the audit trail, each on its own throw-away database.

    DATABASE_URL="postgresql://payments:payments@localhost:5432/payments" python -m unittest tests.test_incremental
Skipped automatically when no database is configured.
"""
import csv
import pathlib
import shutil
import sys
import tempfile
import unittest

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dbtools import BASE_URL, ROOT, load, rows, scalar, temp_database  # noqa: E402

DELTAS = ROOT / 'seed' / 'deltas'
DQ_RULES = ROOT / 'db' / 'tests' / 'data_quality.sql'


def last_run(dsn, source_suffix):
    """(status, counts, error) of the most recent run whose source ends with `source_suffix`."""
    return rows(dsn, 'SELECT status, counts, error FROM audit.load_run WHERE source LIKE %s '
                     'ORDER BY started_at DESC LIMIT 1', (f'%{source_suffix}',))[0]


def apply_deltas(dsn, *numbers):
    for n in numbers:
        result = load(dsn, DELTAS / f'delta_{n}', apply=True)
        assert result.returncode == 0, result.stdout + result.stderr


@unittest.skipUnless(BASE_URL, 'DATABASE_URL not set')
class IncrementalLoad(unittest.TestCase):

    def test_running_the_same_file_again_changes_nothing(self):
        with temp_database() as dsn:
            logged = scalar(dsn, 'SELECT count(*) FROM audit.change_log')
            self.assertGreater(logged, 0)
            self.assertEqual(load(dsn, apply=True).returncode, 0)
            status, counts, _ = last_run(dsn, 'seed/data')
            self.assertEqual(status, 'succeeded')
            self.assertEqual(counts, {'orders unchanged': 289, 'suppliers in file': 40, 'treasury movements loaded': 0})
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM audit.change_log'), logged, 'a re-run must not touch the history')

    def test_a_delta_inserts_new_orders_and_updates_changed_ones(self):
        with temp_database() as dsn:
            before = scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago')
            apply_deltas(dsn, 1)
            _, counts, _ = last_run(dsn, 'delta_1')
            self.assertEqual((counts['orders new'], counts['orders changed']), (6, 5))
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), before + 6)

    def test_a_status_change_adds_a_history_row_and_keeps_the_data_quality_rules(self):
        with temp_database() as dsn:
            apply_deltas(dsn, 1, 2)
            multi = scalar(dsn, "SELECT count(*) FROM (SELECT orden_pago_id FROM pagos.orden_pago_historial "
                                "GROUP BY orden_pago_id HAVING count(*) > 1) x")
            self.assertGreaterEqual(multi, 6)
            with psycopg.connect(dsn) as con:
                failing = [(rule, n) for rule, n in con.execute(DQ_RULES.read_text()).fetchall() if n]
            self.assertEqual(failing, [])

    def test_the_change_log_names_the_run_the_fields_and_the_actor(self):
        with temp_database() as dsn:
            apply_deltas(dsn, 1)
            run_id = scalar(dsn, "SELECT run_id FROM audit.load_run WHERE source LIKE '%delta_1' ORDER BY started_at DESC LIMIT 1")
            changes = rows(dsn, "SELECT changed_fields, actor, old_row ->> 'estado_id', new_row ->> 'estado_id' "
                                "FROM audit.change_log WHERE run_id = %s AND table_name = 'pagos.orden_pago' AND operation = 'UPDATE'",
                           (run_id,))
            self.assertEqual(len(changes), 4, 'four orders went from pending to paid')
            for fields, actor, old, new in changes:
                self.assertEqual(actor, 'etl.load_seed')
                self.assertEqual(fields, ['estado_id', 'fecha_pago'])
                self.assertNotEqual(old, new)
            amount = rows(dsn, "SELECT old_row ->> 'importe', new_row ->> 'importe' FROM audit.change_log "
                               "WHERE run_id = %s AND table_name = 'pagos.orden_pago_detalle' AND operation = 'UPDATE'", (run_id,))
            self.assertEqual(len(amount), 1, 'one line amount was corrected')
            self.assertLess(float(amount[0][0]), float(amount[0][1]))

    def test_invalid_rows_go_to_quarantine_and_the_rest_is_loaded(self):
        with temp_database() as dsn:
            apply_deltas(dsn, 1, 2)
            status, counts, _ = last_run(dsn, 'delta_2')
            self.assertEqual(status, 'succeeded_with_rejects')
            self.assertEqual((counts['orders new'], counts['orders changed'], counts['orders unchanged'], counts['rejected order']),
                             (5, 4, 1, 2))
            reasons = sorted(r[0] for r in rows(dsn, 'SELECT reason FROM audit.rejected_row'))
            self.assertEqual(reasons, ['pendiente OP with a payment date', 'supplier not loaded'])
            raw = rows(dsn, "SELECT raw ->> 'proveedor' FROM audit.rejected_row WHERE reason = 'supplier not loaded'")[0][0]
            self.assertEqual(raw, 'Unknown Supplier S.A.', 'the quarantine keeps the row exactly as it arrived')
            self.assertEqual(scalar(dsn, "SELECT count(*) FROM pagos.tercero WHERE razon_social = 'Unknown Supplier S.A.'"), 0)

    def test_a_dry_run_saves_no_data_but_leaves_its_record(self):
        with temp_database() as dsn:
            orders, logged = scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), scalar(dsn, 'SELECT count(*) FROM audit.change_log')
            self.assertEqual(load(dsn, DELTAS / 'delta_1').returncode, 0)
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), orders)
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM audit.change_log'), logged)
            self.assertEqual(rows(dsn, "SELECT mode, status FROM audit.load_run WHERE source LIKE '%delta_1'"), [('dry-run', 'succeeded')])

    def test_too_many_rejected_rows_abort_the_run_and_keep_the_evidence(self):
        with temp_database() as dsn:
            apply_deltas(dsn, 1)
            orders = scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago')
            result = load(dsn, DELTAS / 'delta_2', True, '--max-reject-rate', '0.05')
            self.assertEqual(result.returncode, 1)
            self.assertIn('RUN ABORTED', result.stdout)
            status, _, error = last_run(dsn, 'delta_2')
            self.assertEqual(status, 'failed')
            self.assertIn('above the limit', error)
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), orders, 'nothing from the aborted run was saved')
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM audit.rejected_row'), 2, 'but the rejected rows are kept for diagnosis')

    def test_a_database_error_mid_run_leaves_nothing_and_a_rerun_recovers(self):
        with temp_database() as dsn:
            orders = scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago')
            hashes = scalar(dsn, 'SELECT count(*) FROM audit.source_row')
            broken = pathlib.Path(tempfile.mkdtemp()) / 'delta_broken'
            shutil.copytree(DELTAS / 'delta_1', broken)
            path = broken / 'payment_order_lines.csv'
            with open(path, newline='', encoding='utf-8') as f:
                data = list(csv.reader(f))
            data[5][6] = '1e20'                       # passes the validation but overflows numeric(14,2) in the database
            with open(path, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerows(data)
            try:
                result = load(dsn, broken, apply=True)
                self.assertNotEqual(result.returncode, 0)
                status, _, error = last_run(dsn, 'delta_broken')
                self.assertEqual(status, 'failed')
                self.assertIn('NumericValueOutOfRange', error)
                self.assertEqual(scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), orders)
                self.assertEqual(scalar(dsn, 'SELECT count(*) FROM audit.source_row'), hashes)
                apply_deltas(dsn, 1)                  # the same load, with the right file, goes through
                self.assertEqual(scalar(dsn, 'SELECT count(*) FROM pagos.orden_pago'), orders + 6)
            finally:
                shutil.rmtree(broken.parent, ignore_errors=True)


@unittest.skipUnless(BASE_URL, 'DATABASE_URL not set')
class AuditTrigger(unittest.TestCase):

    def test_an_update_that_changes_nothing_writes_no_history(self):
        with temp_database() as dsn:
            logged = scalar(dsn, 'SELECT count(*) FROM audit.change_log')
            with psycopg.connect(dsn) as con:
                con.execute('UPDATE pagos.orden_pago SET fecha_vencimiento = fecha_vencimiento WHERE id = 1')
            self.assertEqual(scalar(dsn, 'SELECT count(*) FROM audit.change_log'), logged)

    def test_a_manual_edit_is_logged_with_the_actor_set_by_the_caller(self):
        with temp_database() as dsn:
            with psycopg.connect(dsn) as con:
                con.execute("SET LOCAL app.actor = 'analyst'")
                con.execute("UPDATE pagos.orden_pago SET fecha_vencimiento = DATE '2027-01-01' WHERE id = 1")
            fields, actor, run_id = rows(dsn, "SELECT changed_fields, actor, run_id FROM audit.change_log "
                                              "WHERE table_name = 'pagos.orden_pago' AND record_id = 1 AND operation = 'UPDATE'")[0]
            self.assertEqual((fields, actor, run_id), (['fecha_vencimiento'], 'analyst', None))

    def test_the_history_is_append_only(self):
        with temp_database() as dsn:
            with psycopg.connect(dsn) as con:
                with self.assertRaises(psycopg.errors.RaiseException):
                    con.execute("UPDATE audit.change_log SET actor = 'someone else'")
            with psycopg.connect(dsn) as con:
                with self.assertRaises(psycopg.errors.RaiseException):
                    con.execute('DELETE FROM audit.change_log')

    def test_a_delete_is_logged_with_the_old_row(self):
        with temp_database() as dsn:
            with psycopg.connect(dsn) as con:
                con.execute('DELETE FROM pagos.orden_pago_detalle WHERE id = (SELECT min(id) FROM pagos.orden_pago_detalle)')
            old_amount = scalar(dsn, "SELECT old_row ->> 'importe' FROM audit.change_log "
                                     "WHERE table_name = 'pagos.orden_pago_detalle' AND operation = 'DELETE'")
            self.assertIsNotNone(old_amount)


if __name__ == '__main__':
    unittest.main()
