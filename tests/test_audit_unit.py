"""Unit tests (no database) for the traceability helpers and the row-level validation rules."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'etl'))
from audit import canonical, classify, files_hash, row_hash  # noqa: E402
from validators import is_iso_date, order_problems  # noqa: E402

METHODS = ('Transferencia', 'Echeq', 'Efectivo', 'Pago online')
STATUSES = ('pendiente', 'pagada', 'anulada')
GOOD_ORDER = {'empresa': 'Alpha', 'nro_op': '1', 'fecha_op': '2026-01-05', 'proveedor': 'X', 'cuit': '30000012179',
              'forma_pago': 'Transferencia', 'estado': 'pendiente', 'fecha_pago': '', 'fecha_vencimiento': '', 'cbu': ''}
GOOD_LINES = [{'renglon': '1', 'importe': '100.00'}]


def problems(order=None, lines=None, **kw):
    options = dict(company_known=True, supplier_known=True, methods=METHODS, statuses=STATUSES)
    options.update(kw)
    return order_problems({**GOOD_ORDER, **(order or {})}, GOOD_LINES if lines is None else lines, **options)


class Hashing(unittest.TestCase):
    def test_same_content_gives_the_same_hash_whatever_the_key_order(self):
        self.assertEqual(row_hash({'a': 1, 'b': [1, 2]}), row_hash({'b': [1, 2], 'a': 1}))
        self.assertEqual(canonical({'b': 1, 'a': 2}), canonical({'a': 2, 'b': 1}))

    def test_any_change_in_the_content_changes_the_hash(self):
        self.assertNotEqual(row_hash({'importe': '100.00'}), row_hash({'importe': '100.01'}))

    def test_files_hash_follows_the_content_of_the_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 'a.csv'
            path.write_text('x\n1\n')
            first = files_hash(d)
            self.assertEqual(first, files_hash(d))
            path.write_text('x\n2\n')
            self.assertNotEqual(first, files_hash(d))


class Classification(unittest.TestCase):
    def test_never_loaded_is_new(self):
        self.assertEqual(classify(None, 'abc'), 'new')

    def test_same_hash_is_unchanged(self):
        self.assertEqual(classify('abc', 'abc'), 'unchanged')

    def test_other_hash_is_changed(self):
        self.assertEqual(classify('abc', 'def'), 'changed')


class OrderRules(unittest.TestCase):
    def test_a_valid_order_has_no_problems(self):
        self.assertEqual(problems(), [])

    def test_paid_order_needs_a_payment_date(self):
        self.assertIn('paid OP without a payment date', problems({'estado': 'pagada'}))
        self.assertEqual(problems({'estado': 'pagada', 'fecha_pago': '2026-01-06'}), [])

    def test_unpaid_order_cannot_have_a_payment_date(self):
        self.assertIn('pendiente OP with a payment date', problems({'fecha_pago': '2026-01-06'}))

    def test_order_without_lines_or_supplier(self):
        self.assertIn('OP without lines', problems(lines=[]))
        self.assertIn('supplier not loaded', problems(supplier_known=False))

    def test_unknown_company_method_and_status(self):
        self.assertIn('unknown company', problems(company_known=False))
        self.assertTrue(any('payment method' in p for p in problems({'forma_pago': 'Bitcoin'})))
        self.assertTrue(any('unknown status' in p for p in problems({'estado': 'perdida'})))

    def test_bad_dates(self):
        self.assertIn('invalid date in fecha_op', problems({'fecha_op': '2026-13-40'}))
        self.assertFalse(is_iso_date('05/01/2026'))

    def test_line_amounts_and_numbers(self):
        self.assertIn('line with a zero amount', problems(lines=[{'renglon': '1', 'importe': '0.00'}]))
        self.assertTrue(any('invalid line amount' in p for p in problems(lines=[{'renglon': '1', 'importe': 'abc'}])))
        self.assertTrue(any('duplicate line number' in p for p in problems(
            lines=[{'renglon': '1', 'importe': '1'}, {'renglon': '1', 'importe': '2'}])))
        self.assertEqual(problems(lines=[{'renglon': '1', 'importe': '100'}, {'renglon': '2', 'importe': '-5'}]), [])

    def test_every_reason_is_reported_not_just_the_first(self):
        self.assertGreaterEqual(len(problems({'estado': 'pagada'}, lines=[], supplier_known=False)), 3)


if __name__ == '__main__':
    unittest.main()
