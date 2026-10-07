UV_BACKEND=uv --cache-dir .uv-cache

.PHONY: backend-check backend-build backend-dev backend-test backend-lint backend-format \
	backend-migrate backend-revision backend-drift backend-cleanup services-up services-down

services-up:
	docker compose up -d postgres redis

services-down:
	docker compose down

backend-dev:
	cd backend && $(UV_BACKEND) run uvicorn app.main:app --reload

backend-test:
	cd backend && $(UV_BACKEND) run pytest

backend-lint:
	cd backend && $(UV_BACKEND) run ruff check .

backend-format:
	cd backend && $(UV_BACKEND) run ruff format .

# Fails if the app cannot be constructed. Catches dependency-injection
# mistakes and missing extras, which is how the service became unstartable.
backend-build:
	cd backend && $(UV_BACKEND) run python -c "from app.main import create_app; create_app()"

backend-migrate:
	cd backend && $(UV_BACKEND) run alembic upgrade head

backend-revision:
	cd backend && $(UV_BACKEND) run alembic revision --autogenerate -m "$(name)"

# Fails if a model changed without a matching migration.
backend-drift:
	cd backend && $(UV_BACKEND) run python scripts/check_schema_drift.py

backend-cleanup:
	cd backend && $(UV_BACKEND) run python -m app.workers.cleanup

# The gate. Run this before pushing; CI runs the same steps.
backend-check: backend-lint backend-build backend-test
	@echo "Backend checks passed."
