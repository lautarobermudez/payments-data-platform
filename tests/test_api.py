"""Tests for the REST API against a real PostgreSQL: the baseline seed plus both incremental file sets, read through the
API role (api_reader) exactly as in production. One shared database for the whole class: the API never writes.

    DATABASE_URL="postgresql://payments:payments@localhost:5432/payments" python -m unittest tests.test_api
Skipped automatically when no database is configured.
"""
import contextlib
import json
import pathlib
import sys
import unittest
from unittest import mock

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dbtools import BASE_URL, ROOT, load, rows, scalar, temp_database  # noqa: E402

DELTAS = ROOT / 'seed' / 'deltas'
AS_OF = '2026-11-01'


def api_dsn(dsn):
    info = conninfo_to_dict(dsn)
    info.update(user='api_reader', password='api_reader')
    return make_conninfo(**info)


@unittest.skipUnless(BASE_URL, 'DATABASE_URL not set')
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from api.main import create_app
        cls.stack = contextlib.ExitStack()
        cls.dsn = cls.stack.enter_context(temp_database())
        for n in (1, 2):
            result = load(cls.dsn, DELTAS / f'delta_{n}', apply=True)
            assert result.returncode == 0, result.stdout + result.stderr
        cls.app = create_app(api_dsn(cls.dsn))
        cls.client = cls.stack.enter_context(TestClient(cls.app))

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()          # closes the API pool first, then drops the database

    def get(self, path, expect=200, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    # ---------- contract ----------
    def test_health(self):
        self.assertEqual(self.get('/health'), {'status': 'ok', 'database': 'up'})

    def test_when_the_database_is_down_the_api_answers_503(self):
        with mock.patch.object(self.app.state.db, 'one', side_effect=psycopg.OperationalError('down')):
            body = self.get('/health', expect=503)
        self.assertEqual(body, {'detail': 'The database is not available'})

    def test_the_published_contract_is_read_only(self):
        spec = self.get('/openapi.json')
        self.assertEqual(spec['info']['title'], 'Payments Data Platform API')
        for path, methods in spec['paths'].items():
            self.assertEqual(list(methods), ['get'], f'{path} must only allow GET')
        self.assertGreaterEqual(len(spec['paths']), 10)

    # ---------- orders ----------
    def test_orders_total_matches_the_database_and_money_is_a_number(self):
        page = self.get('/orders', limit=1)
        self.assertEqual(page['total'], scalar(self.dsn, 'SELECT count(*) FROM pagos.orden_pago'))
        self.assertIsInstance(page['items'][0]['total'], float)

    def test_pagination_walks_every_order_exactly_once(self):
        seen, offset = [], 0
        while True:
            page = self.get('/orders', limit=100, offset=offset, sort='order_date')
            seen += [o['id'] for o in page['items']]
            offset += 100
            if offset >= page['total']:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), scalar(self.dsn, 'SELECT count(*) FROM pagos.orden_pago'))

    def test_filters(self):
        pending = self.get('/orders', status='pendiente', limit=200)
        self.assertEqual(pending['total'], scalar(self.dsn, "SELECT count(*) FROM pagos.v_orden_pago WHERE estado = 'pendiente'"))
        self.assertTrue(all(o['status'] == 'pendiente' for o in pending['items']))
        company = self.get('/orders', company='Beta Foods S.A.', limit=200)['items']
        self.assertTrue(company and all(o['company'] == 'Beta Foods S.A.' for o in company))
        by_name = self.get('/orders', supplier='ALIMENTOS', limit=200)['items']
        self.assertTrue(by_name and all('alimentos' in o['supplier'].lower() for o in by_name))
        window = self.get('/orders', date_from='2026-03-01', date_to='2026-03-31', limit=200)['items']
        self.assertTrue(window and all('2026-03-01' <= o['order_date'] <= '2026-03-31' for o in window))
        big = self.get('/orders', min_total=1000000, limit=200)['items']
        self.assertTrue(big and all(o['total'] >= 1000000 for o in big))
        due = self.get('/orders', due_before='2026-02-01', limit=200)['items']
        self.assertTrue(due and all(o['due_on'] <= '2026-02-01' for o in due))

    def test_sorting(self):
        totals = [o['total'] for o in self.get('/orders', sort='-total', limit=50)['items']]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_search_wildcards_are_literal(self):
        self.assertEqual(self.get('/orders', supplier='%')['total'], 0)
        self.assertEqual(self.get('/orders', supplier='_')['total'], 0)

    def test_invalid_parameters_are_rejected(self):
        for params in ({'limit': 0}, {'limit': 500}, {'offset': -1}, {'sort': 'bogus'}, {'status': 'lost'}, {'date_from': 'yesterday'}):
            self.get('/orders', expect=422, **params)

    def test_order_detail_has_lines_status_history_and_change_log(self):
        order_id = scalar(self.dsn, 'SELECT orden_pago_id FROM pagos.orden_pago_historial GROUP BY orden_pago_id '
                                    'HAVING count(*) > 1 ORDER BY orden_pago_id LIMIT 1')
        detail = self.get(f'/orders/{order_id}')
        self.assertAlmostEqual(sum(line['amount'] for line in detail['order_lines']), detail['total'], places=2)
        self.assertEqual(detail['lines'], len(detail['order_lines']))
        self.assertEqual([h['status'] for h in detail['status_history']], ['pendiente', 'pagada'])
        operations = [(c['table_name'], c['operation']) for c in detail['change_log']]
        self.assertEqual(operations[0], ('pagos.orden_pago', 'INSERT'))
        update = next(c for c in detail['change_log'] if c['table_name'] == 'pagos.orden_pago' and c['operation'] == 'UPDATE')
        self.assertEqual((update['changed_fields'], update['actor']), (['estado_id', 'fecha_pago'], 'etl.load_seed'))
        self.assertIsNotNone(update['run_id'])

    def test_order_detail_never_shows_bank_data(self):
        order_id = self.get('/orders', limit=1, status='pagada')['items'][0]['id']
        text = json.dumps(self.get(f'/orders/{order_id}')).lower()
        self.assertNotIn('cbu', text)
        self.assertNotIn('cuenta_bancaria', text.replace('cuenta_bancaria_id', ''))

    def test_unknown_order_is_404(self):
        self.get('/orders/999999999', expect=404)

    # ---------- suppliers ----------
    def test_suppliers_with_their_amounts(self):
        page = self.get('/suppliers', limit=200)
        self.assertEqual(page['total'], 40)
        pending = scalar(self.dsn, "SELECT sum(importe_total) FROM pagos.v_orden_pago WHERE estado = 'pendiente'")
        self.assertAlmostEqual(sum(s['pending_amount'] for s in page['items']), float(pending), places=2)

    def test_supplier_search_by_name_and_tax_id(self):
        first = self.get('/suppliers', limit=1)['items'][0]
        self.assertEqual(self.get('/suppliers', search=first['tax_id'])['items'][0]['id'], first['id'])
        self.assertTrue(all('sierra' in s['name'].lower() for s in self.get('/suppliers', search='SIERRA')['items']))

    def test_supplier_statement_running_balance(self):
        supplier = max(self.get('/suppliers', limit=200)['items'], key=lambda s: s['pending_amount'])
        statement = self.get(f'/suppliers/{supplier["id"]}/statement')
        self.assertTrue(statement)
        self.assertAlmostEqual(statement[-1]['running_pending'], supplier['pending_amount'], places=2)
        self.get('/suppliers/999999999/statement', expect=404)

    # ---------- accounts payable ----------
    def test_due_lists_overdue_orders_oldest_first(self):
        page = self.get('/payables/due', as_of=AS_OF, days=0, limit=200)
        expected = scalar(self.dsn, "SELECT count(*) FROM pagos.v_orden_pago WHERE estado = 'pendiente' "
                                    "AND fecha_vencimiento IS NOT NULL AND fecha_vencimiento <= %s", (AS_OF,))
        self.assertEqual(page['total'], expected)
        self.assertTrue(all(i['days_overdue'] >= 0 for i in page['items']))
        dates = [i['due_on'] for i in page['items']]
        self.assertEqual(dates, sorted(dates))
        self.assertGreaterEqual(self.get('/payables/due', as_of=AS_OF, days=60)['total'], page['total'])

    def test_aging_buckets_add_up_to_the_pending_total(self):
        aging = self.get('/payables/aging', as_of=AS_OF)
        pending = scalar(self.dsn, "SELECT sum(importe_total) FROM pagos.v_orden_pago WHERE estado = 'pendiente'")
        self.assertAlmostEqual(aging['totals']['total'], float(pending), places=2)
        for row in aging['rows'] + [aging['totals']]:
            buckets = sum(row[k] for k in ('not_due', 'overdue_1_30', 'overdue_31_60', 'overdue_61_90', 'overdue_over_90', 'no_due_date'))
            self.assertAlmostEqual(buckets, row['total'], places=2)

    def test_the_further_the_reference_day_the_older_the_debt(self):
        near = self.get('/payables/aging', as_of=AS_OF)['totals']
        far = self.get('/payables/aging', as_of='2027-12-31')['totals']
        self.assertGreater(far['overdue_over_90'], near['overdue_over_90'])
        self.assertEqual(far['not_due'], 0)
        one = self.get('/payables/aging', as_of=AS_OF, company='Alpha Trading S.A.')
        self.assertEqual([r['company'] for r in one['rows']], ['Alpha Trading S.A.'])

    # ---------- ETL runs ----------
    def test_runs_list_newest_first_and_filters(self):
        runs = self.get('/runs')
        self.assertEqual(runs['total'], 3)
        started = [r['started_at'] for r in runs['items']]
        self.assertEqual(started, sorted(started, reverse=True))
        rejects = self.get('/runs', status='succeeded_with_rejects')
        self.assertEqual([r['source'].endswith('delta_2') for r in rejects['items']], [True])
        self.assertEqual(self.get('/runs', mode='dry-run')['total'], 0)

    def test_a_run_and_its_quarantined_rows(self):
        run = self.get('/runs', status='succeeded_with_rejects')['items'][0]
        self.assertEqual((run['rejected_rows'], run['counts']['rejected order']), (2, 2))
        detail = self.get(f'/runs/{run["run_id"]}')
        self.assertEqual(detail['run_id'], run['run_id'])
        rejects = self.get(f'/runs/{run["run_id"]}/rejects')
        self.assertEqual(sorted(r['reason'] for r in rejects['items']), ['pendiente OP with a payment date', 'supplier not loaded'])
        self.assertTrue(all('cbu' not in r['raw'] for r in rejects['items']), 'bank details are removed from the quarantine view')
        self.assertTrue(all(r['raw'] for r in rejects['items']))

    def test_unknown_or_malformed_run_ids(self):
        self.get('/runs/00000000-0000-0000-0000-000000000000', expect=404)
        self.get('/runs/00000000-0000-0000-0000-000000000000/rejects', expect=404)
        self.get('/runs/not-a-uuid', expect=422)


@unittest.skipUnless(BASE_URL, 'DATABASE_URL not set')
class ApiRole(unittest.TestCase):
    """The database itself, not the application, is what keeps the API read-only and away from bank data."""

    @classmethod
    def setUpClass(cls):
        cls.stack = contextlib.ExitStack()
        cls.dsn = api_dsn(cls.stack.enter_context(temp_database()))

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def denied(self, sql, error):
        with psycopg.connect(self.dsn) as con:
            with self.assertRaises(error):
                con.execute(sql)

    def test_it_cannot_write(self):
        for sql in ("INSERT INTO pagos.empresa (nombre) VALUES ('x')", 'UPDATE pagos.orden_pago SET fecha_pago = fecha_pago',
                    'DELETE FROM pagos.orden_pago', 'TRUNCATE pagos.orden_pago'):
            with self.subTest(sql=sql):
                self.denied(sql, (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege))

    def test_it_cannot_read_bank_details(self):
        for sql in ('SELECT * FROM pagos.cuenta_bancaria', 'SELECT * FROM pagos.v_proveedor', 'SELECT * FROM audit.change_log',
                    'SELECT * FROM audit.rejected_row', 'SELECT cbu FROM pagos.v_estado_cuenta_proveedor'):
            with self.subTest(sql=sql):
                self.denied(sql, (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedColumn))

    def test_it_can_read_what_the_api_serves(self):
        with psycopg.connect(self.dsn) as con:
            for table in ('pagos.v_orden_pago', 'audit.v_order_change_log', 'audit.v_rejected_row', 'audit.load_run'):
                self.assertIsNotNone(con.execute(f'SELECT count(*) FROM {table}').fetchone())

    def test_its_queries_are_cut_after_five_seconds(self):
        with psycopg.connect(self.dsn) as con:
            self.assertEqual(con.execute('SHOW statement_timeout').fetchone()[0], '5s')


if __name__ == '__main__':
    unittest.main()
