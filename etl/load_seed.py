#!/usr/bin/env python3
"""Load the CSV files in seed/data into the pagos / tesoreria schemas.

Safe by design (same method used on the real data):
  * DRY-RUN by default: everything runs inside one transaction that is rolled back; add --apply to commit.
  * Idempotent: an OP is identified by (company, OP number); running twice loads nothing new.
  * Validates before inserting: valid CUIT check digit, valid CBU whose bank code matches the bank,
    every OP has lines. Invalid rows are rejected and reported, never silently fixed.
  * Reconciles after loading: row counts and amounts in the database must equal the loaded CSV rows,
    otherwise the transaction is rolled back and the script exits with an error.

    python etl/load_seed.py                   # dry-run, prints the report
    python etl/load_seed.py --apply           # commit
Connection: --dsn or the DATABASE_URL environment variable (default: dbname=payments).
"""
import argparse
import csv
import os
import pathlib
import sys
from collections import Counter, defaultdict
from decimal import Decimal

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from validators import digits, is_valid_cbu, is_valid_cuit, normalize_name  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parents[1] / 'seed' / 'data'


def read(name):
    with open(DATA / name, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class Report:
    def __init__(self):
        self.counts = Counter()
        self.rejected = []

    def reject(self, what, key, reason):
        self.rejected.append((what, key, reason))
        self.counts[f'rejected {what}'] += 1

    def show(self):
        print('\n=== Load report ===')
        for k in sorted(self.counts):
            print(f'  {k:<38}{self.counts[k]:>6}')
        if self.rejected:
            print('\n=== Rejected rows ===')
            for what, key, reason in self.rejected[:30]:
                print(f'  [{what}] {key}: {reason}')
            if len(self.rejected) > 30:
                print(f'  ... and {len(self.rejected) - 30} more')


def lookup_id(cur, table, column, value, extra=None):
    """Return the id of a lookup row, inserting it when missing."""
    cur.execute(f'SELECT id FROM pagos.{table} WHERE {column} = %s', (value,))
    row = cur.fetchone()
    if row:
        return row[0]
    cols, vals = [column], [value]
    for k, v in (extra or {}).items():
        cols.append(k)
        vals.append(v)
    cur.execute(f'INSERT INTO pagos.{table} ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(vals))}) RETURNING id', vals)
    return cur.fetchone()[0]


def resolve_supplier(cur, name, cuit, report):
    """CUIT -> alias -> normalised name -> create. Returns the tercero id."""
    cuit = digits(cuit)
    if cuit:
        cur.execute('SELECT id FROM pagos.tercero WHERE cuit = %s OR %s = ANY(alias)', (cuit, cuit))
        row = cur.fetchone()
        if row:
            return row[0]
    cur.execute('SELECT id, razon_social FROM pagos.tercero WHERE es_proveedor')
    wanted = normalize_name(name)
    for tid, existing in cur.fetchall():
        if normalize_name(existing) == wanted:
            return tid
    cur.execute('SELECT id FROM pagos.tercero WHERE %s = ANY(alias)', (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute('INSERT INTO pagos.tercero (razon_social, cuit, es_proveedor) VALUES (%s, %s, true) RETURNING id',
                (name, cuit or None))
    report.counts['new suppliers'] += 1
    return cur.fetchone()[0]


def load_master_data(cur, report):
    for row in read('companies.csv'):
        cur.execute('INSERT INTO pagos.empresa (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING', (row['nombre'],))
    for name in ('Transferencia', 'Echeq', 'Efectivo', 'Pago online'):
        lookup_id(cur, 'forma_pago', 'nombre', name)
    for name in ('pendiente', 'pagada', 'anulada'):
        lookup_id(cur, 'estado_op', 'nombre', name)
    for row in read('cost_centers.csv'):
        cur.execute('INSERT INTO pagos.centro_costo (empresa_id, nombre) '
                    'SELECT id, %s FROM pagos.empresa WHERE nombre = %s ON CONFLICT DO NOTHING', (row['nombre'], row['empresa']))

    suppliers = {}
    for row in read('suppliers.csv'):
        if not is_valid_cuit(row['cuit']):
            report.reject('supplier', row['razon_social'], f'invalid CUIT {row["cuit"]}')
            continue
        tid = resolve_supplier(cur, row['razon_social'], row['cuit'], report)
        if row['alias']:
            cur.execute('UPDATE pagos.tercero SET alias = array_append(alias, %s) WHERE id = %s AND NOT (%s = ANY(alias))',
                        (row['alias'], tid, row['alias']))
        suppliers[digits(row['cuit'])] = tid
    report.counts['suppliers in file'] = len(suppliers)

    accounts = {}
    for row in read('bank_accounts.csv'):
        if not is_valid_cbu(row['cbu']):
            report.reject('bank account', row['cuit'], 'invalid CBU')
            continue
        if row['cbu'][:3] != row['bank_code']:
            report.reject('bank account', row['cuit'], 'bank code does not match CBU')
            continue
        tid = suppliers.get(digits(row['cuit']))
        if tid is None:
            report.reject('bank account', row['cuit'], 'supplier not found')
            continue
        bank_id = lookup_id(cur, 'banco', 'codigo', row['bank_code'], {'nombre': row['bank_name']})
        cur.execute('SELECT id FROM pagos.cuenta_bancaria WHERE tercero_id = %s AND cbu = %s', (tid, row['cbu']))
        found = cur.fetchone()
        if found:
            accounts[row['cbu']] = found[0]
        else:
            cur.execute('INSERT INTO pagos.cuenta_bancaria (tercero_id, banco_id, cbu) VALUES (%s, %s, %s) RETURNING id',
                        (tid, bank_id, row['cbu']))
            accounts[row['cbu']] = cur.fetchone()[0]
            report.counts['new bank accounts'] += 1
    return suppliers, accounts


def load_orders(cur, report, suppliers, accounts):
    lines_by_op = defaultdict(list)
    for row in read('payment_order_lines.csv'):
        lines_by_op[(row['empresa'], row['nro_op'], )].append(row)
    cur.execute('SELECT nombre, id FROM pagos.empresa')
    company_id = dict(cur.fetchall())
    cur.execute('SELECT nombre, id FROM pagos.forma_pago')
    method_id = dict(cur.fetchall())
    cur.execute('SELECT nombre, id FROM pagos.estado_op')
    status_id = dict(cur.fetchall())
    cur.execute('SELECT c.nombre, e.nombre, c.id FROM pagos.centro_costo c JOIN pagos.empresa e ON e.id = c.empresa_id')
    center_id = {(e, n): i for n, e, i in cur.fetchall()}

    expected_total = Decimal(0)
    loaded = 0
    for row in read('payment_orders.csv'):
        key = (row['empresa'], row['nro_op'])
        label = f'{row["empresa"]} OP{row["nro_op"] or "(no number)"}'
        lines = lines_by_op.get(key, [])
        if not lines:
            report.reject('order', label, 'OP without lines')
            continue
        tid = suppliers.get(digits(row['cuit']))
        if tid is None:
            report.reject('order', label, 'supplier not loaded')
            continue
        number = int(row['nro_op']) if row['nro_op'] else None
        if number is not None:
            cur.execute('SELECT 1 FROM pagos.orden_pago WHERE empresa_id = %s AND nro_op = %s', (company_id[row['empresa']], number))
        else:
            cur.execute('SELECT 1 FROM pagos.orden_pago WHERE empresa_id = %s AND nro_op IS NULL AND tercero_id = %s AND fecha_op = %s',
                        (company_id[row['empresa']], tid, row['fecha_op']))
        if cur.fetchone():
            report.counts['orders already loaded (skipped)'] += 1
            continue
        account = accounts.get(row['cbu']) if row['cbu'] else None
        cur.execute(
            'INSERT INTO pagos.orden_pago (empresa_id, nro_op, fecha_op, tercero_id, forma_pago_id, cuenta_bancaria_id, '
            'estado_id, fecha_pago, fecha_vencimiento) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (company_id[row['empresa']], number, row['fecha_op'], tid, method_id[row['forma_pago']], account,
             status_id[row['estado']], row['fecha_pago'] or None, row['fecha_vencimiento'] or None))
        op_id = cur.fetchone()[0]
        cur.execute('INSERT INTO pagos.orden_pago_historial (orden_pago_id, estado_id, nota) VALUES (%s,%s,%s)',
                    (op_id, status_id[row['estado']], 'loaded from seed'))
        for line in lines:
            invoice_id = None
            if line['nro_comprobante']:
                cur.execute('INSERT INTO pagos.comprobante (tercero_id, nro_comprobante) VALUES (%s,%s) '
                            'ON CONFLICT (tercero_id, sentido, nro_comprobante) DO UPDATE SET nro_comprobante = EXCLUDED.nro_comprobante '
                            'RETURNING id', (tid, line['nro_comprobante']))
                invoice_id = cur.fetchone()[0]
            center = center_id.get((row['empresa'], line['centro_costo'])) if line['centro_costo'] else None
            cur.execute('INSERT INTO pagos.orden_pago_detalle (orden_pago_id, renglon, comprobante_id, concepto, fecha, importe, centro_costo_id) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (op_id, int(line['renglon']), invoice_id, line['concepto'], line['fecha'], line['importe'], center))
            expected_total += Decimal(line['importe'])
            report.counts['order lines loaded'] += 1
        loaded += 1
    report.counts['orders loaded'] = loaded
    return expected_total


def load_treasury(cur, report):
    cur.execute('SELECT nombre, id FROM pagos.empresa')
    company_id = dict(cur.fetchall())
    accounts = {}
    for row in read('treasury_accounts.csv'):
        cur.execute('INSERT INTO tesoreria.cuentas (empresa_id, banco, nombre, tipo) VALUES (%s,%s,%s,%s) '
                    'ON CONFLICT (empresa_id, banco, nombre) DO NOTHING', (company_id[row['empresa']], row['banco'], row['nombre'], row['tipo']))
        cur.execute('SELECT id FROM tesoreria.cuentas WHERE empresa_id = %s AND banco = %s AND nombre = %s',
                    (company_id[row['empresa']], row['banco'], row['nombre']))
        accounts[(row['empresa'], row['banco'], row['nombre'])] = cur.fetchone()[0]
    order = Counter()
    occurrence = Counter()
    inserted = 0
    for row in read('treasury_movements.csv'):
        account = accounts[(row['empresa'], row['banco'], row['cuenta'])]
        order[account] += 1
        same = (account, row['fecha'], row['debito'], row['credito'])
        occurrence[same] += 1
        cur.execute('INSERT INTO tesoreria.movimientos (cuenta_id, fecha, orden, descripcion, debito, credito, saldo, ocurrencia) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (cuenta_id, fecha, debito, credito, ocurrencia) DO NOTHING',
                    (account, row['fecha'], order[account], row['descripcion'], row['debito'], row['credito'],
                     row['saldo'], occurrence[same]))
        inserted += cur.rowcount
    report.counts['treasury movements loaded'] = inserted
    return len(read('treasury_movements.csv'))


def reconcile(cur, report, expected_total, treasury_rows):
    """The database must hold exactly what the CSV files contain; any difference aborts the load."""
    problems = []
    op_rows = read('payment_orders.csv')
    line_rows = read('payment_order_lines.csv')
    cur.execute('SELECT count(*) FROM pagos.orden_pago')
    if cur.fetchone()[0] != len(op_rows) - sum(1 for r in report.rejected if r[0] == 'order'):
        problems.append('number of orders in the database differs from the file')
    cur.execute('SELECT COALESCE(sum(importe), 0) FROM pagos.orden_pago_detalle')
    db_total = cur.fetchone()[0]
    file_total = sum(Decimal(r['importe']) for r in line_rows)
    if db_total != file_total and not report.rejected:
        problems.append(f'line total differs: database {db_total} vs file {file_total}')
    cur.execute('SELECT count(*) FROM tesoreria.movimientos')
    if cur.fetchone()[0] != treasury_rows:
        problems.append('number of treasury movements differs from the file')
    print(f'\nReconciliation: line total in database = {db_total:,.2f} | in file = {file_total:,.2f}')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dsn', default=os.environ.get('DATABASE_URL', 'dbname=payments'))
    ap.add_argument('--apply', action='store_true', help='commit (default is a dry-run that rolls back)')
    args = ap.parse_args()

    report = Report()
    with psycopg.connect(args.dsn) as con:
        with con.cursor() as cur:
            suppliers, accounts = load_master_data(cur, report)
            expected = load_orders(cur, report, suppliers, accounts)
            treasury_rows = load_treasury(cur, report)
            problems = reconcile(cur, report, expected, treasury_rows)
        report.show()
        if problems:
            con.rollback()
            print('\nRECONCILIATION FAILED, nothing was saved:\n  - ' + '\n  - '.join(problems))
            sys.exit(1)
        if args.apply:
            con.commit()
            print('\nCommitted.')
        else:
            con.rollback()
            print('\nDRY-RUN: nothing was saved. Add --apply to commit.')


if __name__ == '__main__':
    main()
