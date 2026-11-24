# Usage: make demo          everything, from an empty machine (needs Docker, psql and Python 3.10+ with requirements.txt installed)
#        make up init seed load deltas load-deltas summary api test      the same, step by step
# Port 5432 taken by another PostgreSQL?  make demo PG_PORT=5433
PG_PORT ?= 5432
export PG_PORT
export DATABASE_URL ?= postgresql://payments:payments@localhost:$(PG_PORT)/payments
API_DATABASE_URL ?= postgresql://api_reader:api_reader@localhost:$(PG_PORT)/payments
PY ?= python3

up:            ## start PostgreSQL in Docker
	docker compose up -d --wait

init:          ## create schemas, tables, views, the audit trail and the read-only API role
	for f in db/01_schema.sql db/02_views.sql db/03_bi_views.sql db/04_treasury.sql db/05_audit.sql db/06_api_role.sql; do \
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

api:           ## run the read-only REST API on http://127.0.0.1:8000 (interactive docs at /docs)
	API_DATABASE_URL=$(API_DATABASE_URL) $(PY) -m uvicorn api.main:app --port 8000

summary:       ## what the loads and the audit trail recorded
	psql "$(DATABASE_URL)" -c "SELECT started_at::time(0) AS at, mode, status, source, counts::text FROM audit.load_run ORDER BY started_at"
	psql "$(DATABASE_URL)" -c "SELECT reason, count(*) AS rows FROM audit.rejected_row GROUP BY reason ORDER BY reason"
	psql "$(DATABASE_URL)" -c "SELECT table_name, operation, count(*) AS rows FROM audit.change_log GROUP BY 1, 2 ORDER BY 1, 2"

demo: up init seed deltas load load-deltas summary   ## everything from an empty machine, then the API
	@echo
	@echo "API ready: http://127.0.0.1:8000/docs     Try: curl 'http://127.0.0.1:8000/payables/aging?as_of=2026-11-01'"
	@echo "Stop with Ctrl-C; remove the database with: make reset"
	@$(MAKE) --no-print-directory api

test:          ## unit tests + data-quality rules + incremental-load integration tests
	$(PY) -m unittest discover -s tests -v

reset:         ## drop everything and start over
	docker compose down -v

.PHONY: up init seed load deltas load-deltas summary demo api test reset
