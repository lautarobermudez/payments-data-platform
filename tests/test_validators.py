"""Unit tests for the pure validators (no database needed): python -m unittest discover -s tests"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'etl'))
from validators import digits, is_valid_cbu, is_valid_cuit, make_cbu, normalize_name  # noqa: E402


class CuitTests(unittest.TestCase):
    def test_valid_cuit(self):
        self.assertTrue(is_valid_cuit('30-00001000-2'))     # fictitious but structurally valid

    def test_wrong_check_digit_is_rejected(self):
        self.assertFalse(is_valid_cuit('30-00001000-3'))

    def test_wrong_length_is_rejected(self):
        self.assertFalse(is_valid_cuit('300000100'))

    def test_separators_are_ignored(self):
        self.assertEqual(digits('30-0000.1000 2'), '30000010002')


class CbuTests(unittest.TestCase):
    def test_generated_cbu_is_valid_and_keeps_the_bank_code(self):
        cbu = make_cbu('007', '9999', '1234567890123')
        self.assertEqual(len(cbu), 22)
        self.assertTrue(cbu.startswith('007'))
        self.assertTrue(is_valid_cbu(cbu))

    def test_changing_one_digit_breaks_it(self):
        cbu = make_cbu('011', '9999', '0000000000001')
        broken = cbu[:10] + ('1' if cbu[10] != '1' else '2') + cbu[11:]
        self.assertFalse(is_valid_cbu(broken))


class NameTests(unittest.TestCase):
    def test_normalisation_ignores_accents_case_and_punctuation(self):
        self.assertEqual(normalize_name('  Logística   del Sur, S.A. '), 'LOGISTICA DEL SUR S A')

    def test_empty_input(self):
        self.assertEqual(normalize_name(None), '')


if __name__ == '__main__':
    unittest.main()
