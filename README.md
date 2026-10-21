# payments-data-platform

[![CI](https://github.com/lautarobermudez/payments-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/lautarobermudez/payments-data-platform/actions/workflows/ci.yml)

A PostgreSQL data model and load pipeline for the **payment orders** of a small group of companies: who pays, to whom, how, how much, and in which status. It also holds bank movements and exposes flat views for BI tools.

I built the original for a real group of companies, replacing spreadsheets that lived in folders. This repository is a clean rebuild of that design on **fully synthetic data**: no real names, tax ids, bank accounts or amounts.

## What it shows

- **Relational modelling:** 14 tables in 3 schemas, with constraints that carry business rules (CUIT and CBU formats, partial unique indexes, triggers).
- **A safe ETL:** dry-run by default, validation before insert, idempotent re-runs, and a reconciliation step that rolls everything back if the database does not match the source.
- **Data quality as tests:** 13 SQL rules that must return zero violations, run in CI.
- **A BI-ready layer:** a `bi` schema of stable, flat views that Tableau and Power BI connect to (see the companion repository, [payments-bi-dashboards](https://github.com/lautarobermudez/payments-bi-dashboards)).
- **Reproducibility:** a deterministic seed generator and CI that proves it.

## Architecture

```mermaid
flowchart LR
    G[seed/generate_seed.py<br/>deterministic fake data] --> C[(seed/data/*.csv)]
    C --> E[etl/load_seed.py<br/>validate · load · reconcile]
    E --> P[(pagos<br/>12 tables)]
    E --> T[(tesoreria<br/>2 tables)]
    P --> V[pagos.v_* views<br/>ERP-style queries]
    P --> B[bi.* views]
    T --> B
    B --> D[Tableau / Power BI]
    P --> Q[tests: 13 data-quality rules]
```

## Quick start

Requirements: Docker, `psql`, Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
make up        # PostgreSQL 17 in Docker
make init      # schemas, tables, views
make seed      # generate the fictitious dataset
make load      # dry-run, then commit
make test      # unit tests + data-quality rules
```

Then explore, for example:

```bash
psql postgresql://payments:payments@localhost:5432/payments -f docs/queries/01_top_suppliers_per_company.sql
```

The loader prints a report and refuses to commit when the database and the source files disagree:

```
Reconciliation: line total in database = 75,016,726.43 | in file = 75,016,726.43
=== Load report ===
  new suppliers                             40
  orders loaded                            277
  order lines loaded                       464
  treasury movements loaded                421
```

## Repository layout

```
db/            numbered SQL: 01 schema · 02 ERP-style views · 03 BI views · 04 treasury
db/tests/      data-quality rules (each query returns the rows that break a rule)
seed/          deterministic generator and the CSV files it writes
etl/           loader and pure validators (CUIT / CBU check digits, name normalisation)
tests/         unit tests (no database) and the data-quality runner
docs/          data model, ER diagram, glossary, example queries
.github/       CI: schema, seed reproducibility, load twice, tests
```

## Notes

- **Language of the model:** identifiers keep the Spanish domain vocabulary (`orden_pago`, `tercero`, CUIT, CBU) because they are Argentine concepts with no exact English equivalent. Comments, docs and the code around it are in English; see the glossary in [docs/DATA_MODEL.md](docs/DATA_MODEL.md).
- **Scope:** this covers payments and bank movements. Sales invoicing, cheque management and a service layer (API) are planned extensions, not part of this repository.
- **How it was built:** I designed the model and the rules, and used Claude Code to speed up drafting; I reviewed and validated the output, and the tests and the reconciliation step exist to catch mistakes from any author.

## License

MIT, see [LICENSE](LICENSE).
