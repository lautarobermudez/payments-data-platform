# Usage: make up init seed load deltas load-deltas test      (needs Docker, psql and Python 3.10+)
export DATABASE_URL ?= postgresql://payments:payments@localhost:5432/payments
PY ?= python3

up:            ## start PostgreSQL in Docker
	docker compose up -d --wait

init:          ## create schemas, tables, views and the audit trail
	for f in db/01_schema.sql db/02_views.sql db/03_bi_views.sql db/04_treasury.sql db/05_audit.sql; do \
	  psql "$(DATABASE_URL)" -v ON_ERROR_STOP=1 -q -f $$f || exit 1; done

seed:          ## generate the fictitious dataset (deterministic)
	$(PY) seed/generate_seed.py

load:          ## load the dataset (dry-run first, then commit)
	$(PY) etl/load_seed.py
	$(PY) etl/load_seed.py --apply

deltas:        ## generate the incremental file sets (seed/deltas/delta_1 and delta_2)
	$(PY) seed/generate_seed.py --delta 1
	$(PY) seed/generate_seed.py --delta 2

load-deltas:   ## load the incremental file sets (dry-run, then commit); delta 2 carries 2 invalid rows on purpose
	for n in 1 2; do \
	  $(PY) etl/load_seed.py --source seed/deltas/delta_$$n || exit 1; \
	  $(PY) etl/load_seed.py --source seed/deltas/delta_$$n --apply || exit 1; done

test:          ## unit tests + data-quality rules + incremental-load integration tests
	$(PY) -m unittest discover -s tests -v

reset:         ## drop everything and start over
	docker compose down -v

.PHONY: up init seed load deltas load-deltas test reset
