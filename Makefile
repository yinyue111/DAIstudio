.DEFAULT_GOAL := help
VENV := backend/.venv
PY := $(VENV)/bin

.PHONY: help install install-frontend test lint fmt typecheck migrate audit \
        run-api run-worker run-frontend build-frontend docker-up docker-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

install: ## Create backend venv + install runtime & dev deps
	cd backend && python3 -m venv .venv && \
		.venv/bin/pip install -U pip && \
		.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

install-frontend: ## Install frontend deps
	cd frontend && npm ci

test: ## Run backend tests
	cd backend && .venv/bin/pytest

lint: ## Lint backend (ruff)
	cd backend && .venv/bin/ruff check .

fmt: ## Auto-fix + format backend (ruff)
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

migrate: ## Apply DB migrations (alembic upgrade head)
	cd backend && .venv/bin/alembic upgrade head

audit: ## Audit dependencies for known vulnerabilities (backend + frontend)
	cd backend && .venv/bin/pip install -q pip-audit && .venv/bin/pip-audit
	cd frontend && npm audit --omit=dev --audit-level=high --registry=https://registry.npmjs.org

run-api: ## Run the API (reload)
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

run-worker: ## Run the Celery worker (+ beat for cleanup)
	./scripts/run_worker.sh

run-frontend: ## Run the frontend dev server
	cd frontend && npm run dev

build-frontend: ## Production build of the frontend
	cd frontend && npm run build

docker-up: ## Build & start the full stack (postgres+redis+api+worker+web)
	docker compose up -d --build

docker-down: ## Stop the stack
	docker compose down

clean: ## Remove caches & build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf backend/.pytest_cache frontend/.next backend/celerybeat-schedule*
