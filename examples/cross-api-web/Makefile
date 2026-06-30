API_PORT ?= 8000
WEB_PORT ?= 5173
DB ?= $(CURDIR)/demo.db

.PHONY: help dev api api-no-reload api-reset web test test-api test-web build-web

help:
	@echo "Targets:"
	@echo "  make dev           Run API reloader and Vite dev server"
	@echo "  make api           Run API on :$(API_PORT) with Python auto-reload"
	@echo "  make web           Run Vite on :$(WEB_PORT)"
	@echo "  make api-reset     Reset SQLite seed data, then run API"
	@echo "  make test          Run API tests and web tests/build"
	@echo ""
	@echo "Variables: API_PORT=$(API_PORT) WEB_PORT=$(WEB_PORT) DB=$(DB)"

dev:
	$(MAKE) -j2 api web

api:
	cd api && python demo_server.py --port $(API_PORT) --db $(DB)

api-no-reload:
	cd api && python demo_server.py --no-reload --port $(API_PORT) --db $(DB)

api-reset:
	cd api && python demo_server.py --reset --port $(API_PORT) --db $(DB)

web:
	cd web && npm run dev -- --port $(WEB_PORT)

test: test-api test-web

test-api:
	cd api && python -m pytest -q

test-web:
	cd web && node --test tests/contracts.test.mjs
	cd web && npm run build

build-web:
	cd web && npm run build
