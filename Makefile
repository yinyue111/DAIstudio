.DEFAULT_GOAL := help
VENV := backend/.venv
PY := $(VENV)/bin

.PHONY: help install install-frontend test test-frontend lint fmt compile migrate alembic-check audit compose-check docker-build-check \
        worker-topology-check run-api run-worker run-beat run-frontend build-frontend docker-up docker-down release-check release-check-worktree release-source clean

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

test-frontend: ## Run frontend unit tests
	cd frontend && npm run test:unit

lint: ## Lint backend (ruff)
	cd backend && .venv/bin/ruff check .

fmt: ## Auto-fix + format backend (ruff)
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

compile: ## Compile backend Python sources (syntax/import smoke)
	cd backend && .venv/bin/python -m compileall -q app tests scripts alembic

migrate: ## Apply DB migrations (alembic upgrade head)
	cd backend && .venv/bin/alembic upgrade head

alembic-check: ## Check Alembic migration drift against models
	cd backend && .venv/bin/alembic check

audit: ## Audit dependencies for known vulnerabilities (backend + frontend)
	cd backend && .venv/bin/python -m pip_audit --progress-spinner off
	cd frontend && npm audit --omit=dev --audit-level=high --registry=https://registry.npmjs.org

run-api: ## Run the API (reload)
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

run-worker: ## Run the Celery worker (+ beat for cleanup)
	./scripts/run_worker.sh

run-beat: ## Run the Celery beat scheduler (single instance)
	./scripts/run_beat.sh

run-frontend: ## Run the frontend dev server
	./scripts/run_frontend.sh

build-frontend: ## Production build of the frontend
	cd frontend && npm run build

docker-up: ## Build & start the full stack (postgres+redis+api+worker+web)
	docker compose up -d --build

docker-down: ## Stop the stack
	docker compose down

compose-check: ## Validate docker compose rendering with required placeholders
	POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-release-check-postgres-password}" \
		REDIS_PASSWORD="$${REDIS_PASSWORD:-release-check-redis-password}" \
		BACKEND_ENV_FILE="$${BACKEND_ENV_FILE:-./backend/.env.example}" \
		docker compose config --quiet

docker-build-check: ## Build backend/frontend Docker images without starting services
	POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-release-check-postgres-password}" \
		REDIS_PASSWORD="$${REDIS_PASSWORD:-release-check-redis-password}" \
		BACKEND_ENV_FILE="$${BACKEND_ENV_FILE:-./backend/.env.example}" \
		docker compose build api worker worker_image worker_video worker_video_download worker_parse beat frontend

worker-topology-check: ## Validate isolated worker roles and compose services
	./scripts/test_worker_parallelism.sh

release-check: compile lint migrate alembic-check test test-frontend build-frontend compose-check worker-topology-check docker-build-check audit ## Run local release gates against the same clean HEAD artifact as CI
	@test -z "$$(git status --porcelain)" || \
		(echo "release-check archives HEAD; commit or stash worktree changes first, or use release-check-worktree" >&2; exit 1)
	tmp="$$(mktemp -d)" && \
		git archive --format=tar.gz --output="$$tmp/ai-studio-source.tar.gz" HEAD && \
		python3 scripts/check_release_artifact.py "$$tmp/ai-studio-source.tar.gz" && \
		rm -rf "$$tmp"
	find . -maxdepth 3 \( -name .venv -o -name .next -o -name node_modules \) -type d -print | sort

release-check-worktree: compile lint migrate alembic-check test test-frontend build-frontend compose-check worker-topology-check docker-build-check audit ## Run release gates against tracked + untracked worktree files
	tmp="$$(mktemp -d)" && \
		deleted="$$(git ls-files --deleted)" && \
		if [ -n "$$deleted" ]; then \
			echo "release-check-worktree: tracked files are deleted in the worktree; commit the deletion or restore them first:" >&2; \
			printf '%s\n' "$$deleted" >&2; \
			rm -rf "$$tmp"; \
			exit 1; \
		fi && \
		git ls-files -z --cached --others --exclude-standard | \
			COPYFILE_DISABLE=1 tar --null -czf "$$tmp/ai-studio-source.tar.gz" --files-from - && \
		python3 scripts/check_release_artifact.py "$$tmp/ai-studio-source.tar.gz" && \
		rm -rf "$$tmp"
	find . -maxdepth 3 \( -name .venv -o -name .next -o -name node_modules \) -type d -print | sort

release-source: ## Build a clean source tarball from git and verify artifact hygiene
	@test -z "$$(git status --porcelain)" || \
		(echo "working tree is dirty; commit or stash changes before building a release artifact" >&2; exit 1)
	mkdir -p dist
	git archive --format=tar.gz --output=dist/ai-studio-source.tar.gz HEAD
	python3 scripts/check_release_artifact.py dist/ai-studio-source.tar.gz

clean: ## Remove caches & build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .venv dist .pytest_cache .ruff_cache backend/.pytest_cache backend/.ruff_cache \
		frontend/.next backend/celerybeat-schedule*
