.DEFAULT_GOAL := help
VENV := backend/.venv
PY := $(VENV)/bin
RELEASE_VERSION ?= launch-lite-20260721
RELEASE_BASENAME := ai-studio-$(RELEASE_VERSION)-source

.PHONY: help install install-frontend test test-evidence-provider test-video-semantic-provider test-audio-provider test-analyzer-providers test-frontend frontend-typecheck reverse-golden-eval lint fmt compile migrate alembic-check audit compose-check docker-build-check docker-build-analyzers \
	        worker-topology-check run-api run-worker run-beat run-frontend build-frontend docker-up docker-down release-check release-check-worktree release-source release-source-worktree clean

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
	cd backend && .venv/bin/python -m pytest

test-evidence-provider: ## Run the self-hosted image evidence provider contract tests
	PYTHONPATH=. backend/.venv/bin/python -m pytest -q evidence_provider/tests

test-video-semantic-provider: ## Run the self-hosted video semantic provider contract tests
	PYTHONPATH=. backend/.venv/bin/python -m pytest -q video_semantic_provider/tests

test-audio-provider: ## Run the self-hosted ASR provider contract tests
	PYTHONPATH=. backend/.venv/bin/python -m pytest -q audio_provider/tests

test-analyzer-providers: test-evidence-provider test-video-semantic-provider test-audio-provider ## Run all optional analyzer provider contract tests

test-frontend: ## Run frontend unit tests
	cd frontend && npm run test:unit

frontend-typecheck: ## Type-check the frontend public contracts and application
	cd frontend && npm run typecheck

reverse-golden-eval: ## Evaluate sanitized offline reverse-prompt golden samples
	cd backend && .venv/bin/python scripts/evaluate_reverse_golden.py tests/fixtures/reverse_golden_samples.json

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
		docker compose build api worker worker_image worker_video worker_video_download worker_parse worker_reverse worker_workflow beat frontend

docker-build-analyzers: ## Build optional image, video semantic and ASR analyzer images
	POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-release-check-postgres-password}" \
		REDIS_PASSWORD="$${REDIS_PASSWORD:-release-check-redis-password}" \
		BACKEND_ENV_FILE="$${BACKEND_ENV_FILE:-./backend/.env.example}" \
		docker compose --profile evidence build evidence_provider video_semantic_provider audio_provider

worker-topology-check: ## Validate isolated worker roles and compose services
	./scripts/test_worker_parallelism.sh

release-check: compile lint migrate alembic-check test test-analyzer-providers reverse-golden-eval test-frontend frontend-typecheck build-frontend compose-check worker-topology-check docker-build-check audit ## Run local release gates against the same clean HEAD artifact as CI
	@test -z "$$(git status --porcelain)" || \
		(echo "release-check archives HEAD; commit or stash worktree changes first, or use release-check-worktree" >&2; exit 1)
	tmp="$$(mktemp -d)" && \
		git archive --format=tar.gz --output="$$tmp/ai-studio-source.tar.gz" HEAD && \
		python3 scripts/check_release_artifact.py "$$tmp/ai-studio-source.tar.gz" && \
		rm -rf "$$tmp"
	find . -maxdepth 3 \( -name .venv -o -name .next -o -name node_modules \) -type d -print | sort

release-check-worktree: compile lint migrate alembic-check test test-analyzer-providers reverse-golden-eval test-frontend frontend-typecheck build-frontend compose-check worker-topology-check docker-build-check audit ## Run release gates against tracked + untracked worktree files
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

release-source-worktree: ## Build a verified source tarball from tracked + untracked worktree files
	@deleted="$$(git ls-files --deleted)"; \
		if [ -n "$$deleted" ]; then \
			echo "release-source-worktree: tracked files are deleted in the worktree:" >&2; \
			printf '%s\n' "$$deleted" >&2; \
			exit 1; \
		fi
	mkdir -p dist
	git ls-files -z --cached --others --exclude-standard | \
		COPYFILE_DISABLE=1 tar --null -czf "dist/$(RELEASE_BASENAME).tar.gz" --files-from -
	python3 scripts/check_release_artifact.py "dist/$(RELEASE_BASENAME).tar.gz"
	cd dist && shasum -a 256 "$(RELEASE_BASENAME).tar.gz" > "$(RELEASE_BASENAME).tar.gz.sha256"
	@echo "release artifact: dist/$(RELEASE_BASENAME).tar.gz"
	@echo "checksum: dist/$(RELEASE_BASENAME).tar.gz.sha256"

clean: ## Remove caches & build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .venv dist .pytest_cache .ruff_cache backend/.pytest_cache backend/.ruff_cache \
		frontend/.next backend/celerybeat-schedule*
