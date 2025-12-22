.PHONY: test test-integration test-full

test:
	docker compose run --rm -v "$(PWD):/app" -w /app \
		-e PYTHONPATH=/app \
		-e DATABASE_URL=sqlite+aiosqlite:///:memory: \
		-e ENCRYPTION_KEY=KeooZFUkuoYlZe6Ic0zPPC_W-s5UgC2vT2dcWbRjL3Y= \
		bot pytest

test-integration:
	docker compose run --rm -v "$(PWD):/app" -w /app \
		-e PYTHONPATH=/app \
		-e INTEGRATION_TESTS=1 \
		-e DATABASE_URL=postgresql+asyncpg://vpn:vpn@db:5432/postgres \
		-e REDIS_URL=redis://redis:6379/0 \
		-e ENCRYPTION_KEY=KeooZFUkuoYlZe6Ic0zPPC_W-s5UgC2vT2dcWbRjL3Y= \
		bot pytest -m integration

test-full: test test-integration
