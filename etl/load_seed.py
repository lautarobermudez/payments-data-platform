#!/usr/bin/env python3
"""Load a folder of CSV files into the pagos / tesoreria schemas, full or incremental.

Safe by design (same method used on the real data):
  * DRY-RUN by default: everything runs inside one transaction that is rolled back; add --apply to commit.
  * Incremental and idempotent: every payment order is identified by (company, OP number) and carries a content hash.
    New orders are inserted, changed ones are updated, unchanged ones are skipped, so a re-run touches nothing.
  * Quarantine instead of failure: an invalid row (bad tax id, unknown supplier, paid OP without a date...) is stored in
    audit.rejected_row with its reason and the load goes on. If too many rows are rejected the whole run is aborted
    (--max-reject-rate), so garbage is never loaded by accident.
  * Reconciles after loading: every source row must be accounted for (new + changed + unchanged + rejected) and the line
    totals in the database must equal the file, otherwise the transaction is rolled back and the script exits with an error.
  * Traceable: each execution is recorded in audit.load_run (also dry-runs and failures), and every row it changes is logged
    in audit.change_log with the run id.

    python etl/load_seed.py                          # dry-run of seed/data, prints the report
    python etl/load_seed.py --apply                  # commit
    python etl/load_seed.py --source seed/deltas/delta_1 --apply     # an incremental file set

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
from audit import Rejection, RunLog, classify, files_hash, row_hash  # noqa: E402
from validators import digits, is_valid_cbu, is_valid_cuit, normalize_name, order_problems  # noqa: E402

DEFAULT_SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'seed' / 'data'
ACTOR = 'etl.load_seed'
PAYMENT_METHODS = ('Transferencia', 'Echeq', 'Efectivo', 'Pago online')
STATUSES = ('pendiente', 'pagada', 'anulada')
ORDER_FIELDS = ('empresa', 'nro_op', 'fecha_op', 'proveedor', 'cuit', 'forma_pago', 'estado', 'fecha_pago',
                'fecha_vencimiento', 'cbu')
LINE_FIELDS = ('renglon', 'nro_comprobante', 'concepto', 'fecha', 'importe', 'centro_costo')


def read(source, name):
    """Rows of a CSV file as dictionaries (empty list if the file is not part of this file set).
    Each row also carries its line number and file name (keys starting with '_') for the quarantine."""
    path = pathlib.Path(source) / name
    if not path.exists():
        return []
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    for number, row in enumerate(rows, start=2):   # line 1 is the header
        row['_line'], row['_file'] = number, name
    return rows


def raw_of(row):
    return {k: v for k, v in row.items() if not k.startswith('_')}


class Report:
    def __init__(self):
        self.counts = Counter()
        self.rejected = []          # list[audit.Rejection]
        self.examined = 0           # source rows looked at (suppliers + bank accounts + orders), for the reject rate

    def reject(self, what, key, reason, row=None):
        row = row or {}
        self.rejected.append(Rejection(what, str(key), reason, row.get('_file', ''), row.get('_line', 0), raw_of(row)))
        self.counts[f'rejected {what}'] += 1

    def rejected_of(self, what):
        return sum(1 for r in self.rejected if r.entity == what)

    def show(self):
        print('\n=== Load report ===')
        for k in sorted(self.counts):
            print(f'  {k:<38}{self.counts[k]:>6}')
        if self.rejected:
            print('\n=== Quarantined rows (audit.rejected_row) ===')
            for r in self.rejected[:30]:
                print(f'  [{r.entity}] {r.source_file}:{r.line_no} {r.key}: {r.reason}')
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


def load_master_data(cur, report, source):
    for row in read(source, 'companies.csv'):
        cur.execute('INSERT INTO pagos.empresa (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING', (row['nombre'],))
    for name in PAYMENT_METHODS:
        lookup_id(cur, 'forma_pago', 'nombre', name)
    for name in STATUSES:
        lookup_id(cur, 'estado_op', 'nombre', name)
    for row in read(source, 'cost_centers.csv'):
        cur.execute('INSERT INTO pagos.centro_costo (empresa_id, nombre) '
                    'SELECT id, %s FROM pagos.empresa WHERE nombre = %s ON CONFLICT DO NOTHING', (row['nombre'], row['empresa']))

    # Start from what the database already knows, so an incremental file set can reference earlier suppliers/accounts.
    cur.execute('SELECT cuit, id FROM pagos.tercero WHERE cuit IS NOT NULL')
    suppliers = dict(cur.fetchall())
    cur.execute('SELECT cbu, id FROM pagos.cuenta_bancaria WHERE cbu IS NOT NULL')
    accounts = dict(cur.fetchall())

    supplier_rows = read(source, 'suppliers.csv')
    report.examined += len(supplier_rows)
    in_file = 0
    for row in supplier_rows:
        if not is_valid_cuit(row['cuit']):
            report.reject('supplier', row['razon_social'], f'invalid CUIT {row["cuit"]}', row)
            continue
        tid = resolve_supplier(cur, row['razon_social'], row['cuit'], report)
        if row['alias']:
            cur.execute('UPDATE pagos.tercero SET alias = array_append(alias, %s) WHERE id = %s AND NOT (%s = ANY(alias))',
                        (row['alias'], tid, row['alias']))
        suppliers[digits(row['cuit'])] = tid
        in_file += 1
    if supplier_rows:
        report.counts['suppliers in file'] = in_file

    account_rows = read(source, 'bank_accounts.csv')
    report.examined += len(account_rows)
    for row in account_rows:
        if not is_valid_cbu(row['cbu']):
            report.reject('bank account', row['cuit'], 'invalid CBU', row)
            continue
        if row['cbu'][:3] != row['bank_code']:
            report.reject('bank account', row['cuit'], 'bank code does not match CBU', row)
            continue
        tid = suppliers.get(digits(row['cuit']))
        if tid is None:
            report.reject('bank account', row['cuit'], 'supplier not found', row)
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


def order_key(row):
    """Natural key of an order: (company, OP number); an OP without number is told apart by supplier and date."""
    if row['nro_op']:
        return f'{row["empresa"]}|{row["nro_op"]}'
    return f'{row["empresa"]}|-|{digits(row["cuit"])}|{row["fecha_op"]}'


def order_content(row, lines):
    """What the hash is computed from: the order and its lines, without the loader's bookkeeping keys."""
    return {'order': {k: row.get(k, '') for k in ORDER_FIELDS},
            'lines': [{k: line.get(k, '') for k in LINE_FIELDS} for line in sorted(lines, key=lambda x: int(x['renglon']))]}


def find_order(cur, company_id, number, tid, order_date):
    if number is not None:
        cur.execute('SELECT id FROM pagos.orden_pago WHERE empresa_id = %s AND nro_op = %s', (company_id, number))
    else:
        cur.execute('SELECT id FROM pagos.orden_pago WHERE empresa_id = %s AND nro_op IS NULL AND tercero_id = %s AND fecha_op = %s',
                    (company_id, tid, order_date))
    found = cur.fetchone()
    return found[0] if found else None


def write_lines(cur, op_id, tid, company, lines, center_id):
    """Insert or update the lines of an order and delete the ones that are no longer in the file."""
    keep = []
    for line in lines:
        invoice_id = None
        if line['nro_comprobante']:
            cur.execute('INSERT INTO pagos.comprobante (tercero_id, nro_comprobante) VALUES (%s,%s) '
                        'ON CONFLICT (tercero_id, sentido, nro_comprobante) DO UPDATE SET nro_comprobante = EXCLUDED.nro_comprobante '
                        'RETURNING id', (tid, line['nro_comprobante']))
            invoice_id = cur.fetchone()[0]
        center = center_id.get((company, line['centro_costo'])) if line['centro_costo'] else None
        cur.execute('INSERT INTO pagos.orden_pago_detalle (orden_pago_id, renglon, comprobante_id, concepto, fecha, importe, centro_costo_id) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s) '
                    'ON CONFLICT (orden_pago_id, renglon) DO UPDATE SET comprobante_id = EXCLUDED.comprobante_id, '
                    'concepto = EXCLUDED.concepto, fecha = EXCLUDED.fecha, importe = EXCLUDED.importe, '
                    'centro_costo_id = EXCLUDED.centro_costo_id',
                    (op_id, int(line['renglon']), invoice_id, line['concepto'], line['fecha'] or None, line['importe'], center))
        keep.append(int(line['renglon']))
    cur.execute('DELETE FROM pagos.orden_pago_detalle WHERE orden_pago_id = %s AND renglon <> ALL(%s)', (op_id, keep))


def load_orders(cur, report, suppliers, accounts, source, run_id):
    """Returns {order id: (number of lines, total)} for every valid order in the file, whether it was new, changed or unchanged."""
    order_rows = read(source, 'payment_orders.csv')
    line_rows = read(source, 'payment_order_lines.csv')
    report.examined += len(order_rows)
    lines_by_op = defaultdict(list)
    for row in line_rows:
        lines_by_op[(row['empresa'], row['nro_op'])].append(row)
    cur.execute('SELECT nombre, id FROM pagos.empresa')
    company_id = dict(cur.fetchall())
    cur.execute('SELECT nombre, id FROM pagos.forma_pago')
    method_id = dict(cur.fetchall())
    cur.execute('SELECT nombre, id FROM pagos.estado_op')
    status_id = dict(cur.fetchall())
    cur.execute('SELECT c.nombre, e.nombre, c.id FROM pagos.centro_costo c JOIN pagos.empresa e ON e.id = c.empresa_id')
    center_id = {(e, n): i for n, e, i in cur.fetchall()}

    expected = {}
    seen = set()
    for row in order_rows:
        key = order_key(row)
        label = f'{row["empresa"]} OP{row["nro_op"] or "(no number)"}'
        lines = lines_by_op.get((row['empresa'], row['nro_op']), [])
        tid = suppliers.get(digits(row['cuit']))
        problems = order_problems(row, lines, company_known=row['empresa'] in company_id, supplier_known=tid is not None,
                                  methods=method_id, statuses=status_id)
        if key in seen:
            problems.append('duplicate OP in the file')
        seen.add(key)
        if problems:
            report.reject('order', label, '; '.join(problems), row)
            continue

        content_hash = row_hash(order_content(row, lines))
        cur.execute("SELECT row_hash FROM audit.source_row WHERE entity = 'order' AND natural_key = %s", (key,))
        stored = cur.fetchone()
        number = int(row['nro_op']) if row['nro_op'] else None
        op_id = find_order(cur, company_id[row['empresa']], number, tid, row['fecha_op'])
        kind = classify(stored[0] if stored else None, content_hash)
        if kind == 'unchanged' and op_id is None:
            kind = 'new'                      # the hash is there but the order was deleted: load it again
        if kind == 'new' and op_id is not None:
            kind = 'changed'                  # loaded by an older version of the loader: bring it in line with the file

        account = accounts.get(row['cbu']) if row['cbu'] else None
        values = (row['fecha_op'], tid, method_id[row['forma_pago']], account, status_id[row['estado']],
                  row['fecha_pago'] or None, row['fecha_vencimiento'] or None)
        if kind == 'new':
            cur.execute('INSERT INTO pagos.orden_pago (fecha_op, tercero_id, forma_pago_id, cuenta_bancaria_id, estado_id, '
                        'fecha_pago, fecha_vencimiento, empresa_id, nro_op) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                        values + (company_id[row['empresa']], number))
            op_id = cur.fetchone()[0]
            cur.execute('INSERT INTO pagos.orden_pago_historial (orden_pago_id, estado_id, nota) VALUES (%s,%s,%s)',
                        (op_id, status_id[row['estado']], f'loaded from {pathlib.Path(source).name}'))
        elif kind == 'changed':
            cur.execute('SELECT estado_id FROM pagos.orden_pago WHERE id = %s', (op_id,))
            old_status = cur.fetchone()[0]
            cur.execute('UPDATE pagos.orden_pago SET fecha_op = %s, tercero_id = %s, forma_pago_id = %s, cuenta_bancaria_id = %s, '
                        'estado_id = %s, fecha_pago = %s, fecha_vencimiento = %s WHERE id = %s', values + (op_id,))
            if old_status != status_id[row['estado']]:       # the status history keeps one row per status change
                cur.execute('INSERT INTO pagos.orden_pago_historial (orden_pago_id, estado_id, nota) VALUES (%s,%s,%s)',
                            (op_id, status_id[row['estado']], f'updated from {pathlib.Path(source).name}'))
        report.counts[f'orders {kind}'] += 1
        if kind != 'unchanged':
            write_lines(cur, op_id, tid, row['empresa'], lines, center_id)
            report.counts['order lines written'] += len(lines)
            cur.execute("INSERT INTO audit.source_row (entity, natural_key, row_hash, first_run_id, last_run_id) "
                        "VALUES ('order', %s, %s, %s, %s) ON CONFLICT (entity, natural_key) DO UPDATE "
                        "SET row_hash = EXCLUDED.row_hash, last_run_id = EXCLUDED.last_run_id, updated_at = now()",
                        (key, content_hash, run_id, run_id))
        expected[op_id] = (len(lines), sum(Decimal(line['importe']) for line in lines))
    return expected


def load_treasury(cur, report, source):
    """Bank movements are append-only; only part of the full file set (an incremental file set may not carry them)."""
    rows = read(source, 'treasury_movements.csv')
    if not rows:
        return None
    cur.execute('SELECT nombre, id FROM pagos.empresa')
    company_id = dict(cur.fetchall())
    accounts = {}
    for row in read(source, 'treasury_accounts.csv'):
        cur.execute('INSERT INTO tesoreria.cuentas (empresa_id, banco, nombre, tipo) VALUES (%s,%s,%s,%s) '
                    'ON CONFLICT (empresa_id, banco, nombre) DO NOTHING', (company_id[row['empresa']], row['banco'], row['nombre'], row['tipo']))
        cur.execute('SELECT id FROM tesoreria.cuentas WHERE empresa_id = %s AND banco = %s AND nombre = %s',
                    (company_id[row['empresa']], row['banco'], row['nombre']))
        accounts[(row['empresa'], row['banco'], row['nombre'])] = cur.fetchone()[0]
    order = Counter()
    occurrence = Counter()
    inserted = 0
    for row in rows:
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
    return len(rows)


def reconcile(cur, report, source, expected, treasury_rows):
    """The database must hold exactly what the source files contain; any difference aborts the load."""
    problems = []
    order_rows = len(read(source, 'payment_orders.csv'))
    accounted = (report.counts['orders new'] + report.counts['orders changed'] + report.counts['orders unchanged']
                 + report.rejected_of('order'))
    if accounted != order_rows:
        problems.append(f'{order_rows} orders in the file but {accounted} accounted for (new + changed + unchanged + rejected)')
    db_total, file_total = Decimal(0), sum(total for _, total in expected.values())
    if expected:
        cur.execute('SELECT orden_pago_id, count(*), sum(importe) FROM pagos.orden_pago_detalle '
                    'WHERE orden_pago_id = ANY(%s) GROUP BY orden_pago_id', (list(expected),))
        in_db = {i: (n, t) for i, n, t in cur.fetchall()}
        db_total = sum(t for _, t in in_db.values())
        wrong = [i for i, want in expected.items() if in_db.get(i) != want]
        if wrong:
            problems.append(f'{len(wrong)} orders whose lines in the database differ from the file (first id: {wrong[0]})')
    if treasury_rows is not None:
        cur.execute('SELECT count(*) FROM tesoreria.movimientos')
        if cur.fetchone()[0] != treasury_rows:
            problems.append('number of treasury movements differs from the file')
    print(f'\nReconciliation: line total in database = {db_total:,.2f} | in file = {file_total:,.2f} '
          f'({len(expected)} orders checked)')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dsn', default=os.environ.get('DATABASE_URL', 'dbname=payments'))
    ap.add_argument('--source', default=str(DEFAULT_SOURCE), help='folder with the CSV files to load (default: seed/data)')
    ap.add_argument('--apply', action='store_true', help='commit (default is a dry-run that rolls back)')
    ap.add_argument('--max-reject-rate', type=float, default=0.20,
                    help='abort the run when more than this share of the source rows is rejected (default 0.20)')
    args = ap.parse_args()
    source = pathlib.Path(args.source)
    if not source.is_dir():
        sys.exit(f'Source folder not found: {source}')

    report = Report()
    run = RunLog(args.dsn, 'apply' if args.apply else 'dry-run', f'{source.parent.name}/{source.name}', files_hash(source))
    status, error, exit_code = 'failed', None, 0
    try:
        with psycopg.connect(args.dsn) as con:
            with con.cursor() as cur:
                # The audit trigger reads these, so every change made by this run is tied to it.
                cur.execute("SELECT set_config('app.actor', %s, true), set_config('app.run_id', %s, true)", (ACTOR, str(run.run_id)))
                suppliers, accounts = load_master_data(cur, report, source)
                expected = load_orders(cur, report, suppliers, accounts, source, run.run_id)
                treasury_rows = load_treasury(cur, report, source)
                problems = reconcile(cur, report, source, expected, treasury_rows)
            rate = len(report.rejected) / report.examined if report.examined else 0.0
            if rate > args.max_reject_rate:
                problems.append(f'{len(report.rejected)} of {report.examined} rows rejected ({rate:.0%}), above the limit of '
                                f'{args.max_reject_rate:.0%}: the source looks wrong, nothing was loaded')
            report.show()
            if problems:
                con.rollback()
                error, exit_code = '; '.join(problems), 1
                print('\nRUN ABORTED, nothing was saved:\n  - ' + '\n  - '.join(problems))
            else:
                status = 'succeeded_with_rejects' if report.rejected else 'succeeded'
                if args.apply:
                    con.commit()
                    print('\nCommitted.')
                else:
                    con.rollback()
                    print('\nDRY-RUN: nothing was saved. Add --apply to commit.')
    except Exception as exc:  # noqa: BLE001 - recorded in the run log, then re-raised
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        run.finish(status, dict(report.counts), error, report.rejected)
        print(f'Run {run.run_id} recorded in audit.load_run ({status}).')
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
