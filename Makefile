# Usage: make up init seed load test      (needs Docker, psql and Python 3.10+)
export DATABASE_URL ?= postgresql://payments:payments@localhost:5432/payments
PY ?= python3

up:            ## start PostgreSQL in Docker
	docker compose up -d --wait

init:          ## create schemas, tables and views
	for f in db/01_schema.sql db/02_views.sql db/03_bi_views.sql db/04_treasury.sql; do \
	  psql "$(DATABASE_URL)" -v ON_ERROR_STOP=1 -q -f $$f || exit 1; done

seed:          ## generate the fictitious dataset (deterministic)
	$(PY) seed/generate_seed.py

load:          ## load the dataset (dry-run first, then commit)
	$(PY) etl/load_seed.py
	$(PY) etl/load_seed.py --apply

test:          ## unit tests + data-quality rules
	$(PY) -m unittest discover -s tests -v

reset:         ## drop everything and start over
	docker compose down -v

.PHONY: up init seed load test reset
