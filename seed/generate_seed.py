#!/usr/bin/env python3
"""Generate a deterministic, fully fictitious dataset in seed/data/*.csv.

Nothing here comes from a real company: names are invented, tax ids are structurally valid but sit in a
reserved-looking range (30-0000NNNN-D), and bank accounts use a fictitious branch (9999).
The same seed always produces the same files, so results in the docs can be reproduced.

    python seed/generate_seed.py            # writes seed/data/*.csv
    python seed/generate_seed.py --seed 7   # a different but equally reproducible dataset
"""
import argparse
import csv
import datetime as dt
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'etl'))
from validators import cuit_check_digit, make_cbu  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / 'data'
START, END = dt.date(2026, 1, 5), dt.date(2026, 10, 20)

COMPANIES = ['Alpha Trading S.A.', 'Beta Foods S.A.', 'Gamma Logistics S.R.L.']
COST_CENTERS = {'Beta Foods S.A.': ['Retail Division', 'Catering Division']}   # only one company splits its spend
BANKS = [('007', 'Banco Galicia'), ('011', 'Banco Nacion'), ('014', 'Banco Provincia'),
         ('017', 'Banco BBVA'), ('027', 'Banco Supervielle'), ('072', 'Banco Santander')]
PAYMENT_METHODS = ['Transferencia', 'Echeq', 'Efectivo', 'Pago online']
STATUSES = ['pendiente', 'pagada', 'anulada']
WORDS_A = ['Andes', 'Pampa', 'Delta', 'Sur', 'Norte', 'Litoral', 'Cuyo', 'Atlantico', 'Patagonia', 'Rio', 'Sierra', 'Mar']
WORDS_B = ['Logistica', 'Alimentos', 'Servicios', 'Insumos', 'Packaging', 'Tecnologia', 'Seguros', 'Mantenimiento',
           'Distribuciones', 'Energia', 'Consultoria', 'Transporte']
SUFFIXES = ['S.A.', 'S.R.L.', 'S.A.S.']
CONCEPTS = ['Monthly service', 'Goods delivery', 'Maintenance', 'Insurance premium', 'Freight', 'Packaging supplies',
            'Consulting fees', 'Utilities', 'Software licence', 'Cleaning']


def fake_cuit(n: int) -> str:
    first_ten = f'30{n:08d}'            # 30-0000NNNN: low range, not used by real companies
    return first_ten + str(cuit_check_digit(first_ten))


def write(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f'{name}: {len(rows)} rows')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    # ---- suppliers (with one alias each for ~20%) and their bank accounts
    suppliers, accounts = [], []
    names = set()
    while len(suppliers) < 40:
        name = f'{rnd.choice(WORDS_A)} {rnd.choice(WORDS_B)} {rnd.choice(SUFFIXES)}'
        if name in names:
            continue
        names.add(name)
        cuit = fake_cuit(1000 + len(suppliers) * 7)
        alias = name.replace('.', '').replace(' S A', ' SA').upper() if rnd.random() < 0.2 else ''
        suppliers.append((name, cuit, alias))
        bank = rnd.choice(BANKS)
        accounts.append((cuit, bank[0], bank[1], make_cbu(bank[0], '9999', f'{rnd.randrange(10**12, 10**13)}')))
    write('suppliers.csv', ['razon_social', 'cuit', 'alias'], suppliers)
    write('bank_accounts.csv', ['cuit', 'bank_code', 'bank_name', 'cbu'], accounts)
    write('companies.csv', ['nombre'], [[c] for c in COMPANIES])
    write('cost_centers.csv', ['empresa', 'nombre'], [[e, n] for e, ns in COST_CENTERS.items() for n in ns])

    # ---- payment orders and lines
    orders, lines = [], []
    next_number = {c: 1 for c in COMPANIES}
    invoice_seq = 0
    shared_invoice = None
    day = START
    while day <= END:
        if day.weekday() < 5:
            for _ in range(rnd.choice([0, 1, 1, 2, 3])):
                company = rnd.choice(COMPANIES)
                supplier = rnd.choice(suppliers)
                method = rnd.choices(PAYMENT_METHODS, weights=[6, 3, 1, 1])[0]
                age = (END - day).days
                status = rnd.choices(STATUSES, weights=[1 if age < 25 else 0.4, 12, 0.3])[0]
                number = next_number[company]
                next_number[company] += 1
                paid_on = day + dt.timedelta(days=rnd.choice([0, 2, 7, 14, 30])) if status == 'pagada' else None
                due = day + dt.timedelta(days=rnd.choice([0, 7, 15, 30])) if rnd.random() < 0.8 else None
                account_cbu = next(a[3] for a in accounts if a[0] == supplier[1]) if method in ('Transferencia',) else ''
                op_key = f'{company}|{number}'
                orders.append([company, number, day.isoformat(), supplier[0], supplier[1], method, status,
                               paid_on.isoformat() if paid_on else '', due.isoformat() if due else '', account_cbu])
                for line_no in range(1, rnd.choice([1, 1, 2, 3]) + 1):
                    invoice_seq += 1
                    invoice = f'A-0001-{invoice_seq:08d}'
                    if shared_invoice and rnd.random() < 0.03 and shared_invoice[0] == supplier[1]:
                        invoice = shared_invoice[1]            # one invoice paid across two OPs
                    amount = round(rnd.lognormvariate(11.5, 1.0), 2)
                    if line_no > 1 and rnd.random() < 0.15:
                        amount = -round(amount * 0.05, 2)      # a discount line
                    center = rnd.choice(COST_CENTERS[company] + ['']) if company in COST_CENTERS else ''
                    lines.append([company, number, line_no, invoice, rnd.choice(CONCEPTS), day.isoformat(), f'{amount:.2f}', center])
                    shared_invoice = (supplier[1], invoice)
        day += dt.timedelta(days=1)
    # two OPs without number (assigned on payment day)
    for k in range(2):
        company = COMPANIES[k]
        sup = suppliers[k]
        orders.append([company, '', END.isoformat(), sup[0], sup[1], 'Transferencia', 'pendiente', '', '', ''])
        lines.append([company, '', 1, f'A-0002-{k + 1:08d}', 'Pending assignment', END.isoformat(), '1000.00', ''])
    write('payment_orders.csv', ['empresa', 'nro_op', 'fecha_op', 'proveedor', 'cuit', 'forma_pago', 'estado',
                                 'fecha_pago', 'fecha_vencimiento', 'cbu'], orders)
    write('payment_order_lines.csv', ['empresa', 'nro_op', 'renglon', 'nro_comprobante', 'concepto', 'fecha',
                                      'importe', 'centro_costo'], lines)

    # ---- treasury: two accounts with a running balance and some identical movements on the same day
    t_accounts = [[COMPANIES[0], 'Banco Galicia', 'Current account 001', 'cuenta corriente'],
                  [COMPANIES[1], 'Banco Nacion', 'Savings account 002', 'caja de ahorro']]
    write('treasury_accounts.csv', ['empresa', 'banco', 'nombre', 'tipo'], t_accounts)
    movements = []
    for acc in t_accounts:
        balance = 5_000_000.00
        day = START
        while day <= END:
            if day.weekday() < 5:
                for _ in range(rnd.choice([0, 1, 2])):
                    description, credit, debit = rnd.choice([
                        ('Transfer received', round(rnd.uniform(50_000, 900_000), 2), 0),
                        ('Supplier payment', 0, round(rnd.uniform(20_000, 700_000), 2)),
                        ('Bank fee', 0, 1_250.00),                      # repeats on purpose: identical movements
                        ('Tax debit', 0, round(rnd.uniform(1_000, 9_000), 2))])
                    balance = round(balance + credit - debit, 2)
                    movements.append([acc[0], acc[1], acc[2], day.isoformat(), description,
                                      f'{debit:.2f}', f'{credit:.2f}', f'{balance:.2f}'])
            day += dt.timedelta(days=1)
    write('treasury_movements.csv', ['empresa', 'banco', 'cuenta', 'fecha', 'descripcion', 'debito', 'credito', 'saldo'], movements)


if __name__ == '__main__':
    main()
