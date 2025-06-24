# VPN Service

This project implements a small VPN management system consisting of a Telegram bot, a simple admin API and a billing daemon. It can manage multiple VPN servers via a REST API, generate client configuration files and handle basic billing based on the number of active configs.

## Technology stack

- **Python 3** with `asyncio`
- [**Aiogram**](https://github.com/aiogram/aiogram) for the Telegram bot
- [**FastAPI**](https://fastapi.tiangolo.com/) for the admin API
- [**SQLAlchemy**](https://www.sqlalchemy.org/) (async) as ORM
- [**Pydantic**](https://docs.pydantic.dev/) for settings, service models and API schemas
- [**Cryptography**](https://cryptography.io/) (Fernet) to store server API keys encrypted
- [**httpx**](https://www.python-httpx.org/) for talking to remote VPN servers
- `pytest` with `pytest-asyncio` for the tests

## Components

### Telegram bot

Located in [`bot/`](bot). It allows users to register, view balance and create VPN configs. A simple FSM guides the user through choosing a server and entering a display name. Config files are sent as temporary files and removed afterwards.

### Admin API

Located in [`admin/`](admin). It exposes a small JSON API to manage servers, users and configs. Endpoints are protected either by an `X-API-Key` or by a login token obtained from `/login`. Start it with:

```bash
uvicorn admin.app:app --host 0.0.0.0 --port 8000
```

The API listens on `http://localhost:8000`.
Request bodies are validated with Pydantic models.

### Billing daemon

`billing_daemon/main.py` uses Redis and RQ to periodically charge users for their active configs. It also sends Telegram notifications when a user's balance falls below 10 and suspends configs when the balance becomes negative. Run it as a standalone process.

### Database

Async SQLAlchemy models live under [`core/db`](core/db). Use `scripts/init_db.py` once to create the schema:

```bash
python scripts/init_db.py
```

## Configuration

All settings are read from environment variables (see `core/config.py`).
Create a `.env` file (see `.env.example`) to provide them.
When using Docker Compose the `DATABASE_URL` value is generated automatically
from the PostgreSQL credentials, otherwise set it manually:

- `DATABASE_URL` – database connection string (only required when running
  without Docker Compose)
- `ENCRYPTION_KEY` – Fernet key used to encrypt server API keys
- `BOT_TOKEN` – Telegram bot token
- `PER_CONFIG_COST` – how much to charge per active config (default `1.0`)
- `CONFIG_CREATION_COST` – cost charged when a config is created (default `10.0`)
- `BILLING_INTERVAL` – seconds between periodic charges
- `ADMIN_USERNAME` – username for `/login`
- `ADMIN_PASSWORD_HASH` – bcrypt hash of the login password
- `REDIS_URL` – Redis connection string for token storage (default `redis://redis:6379/0`)

A helper script `scripts/fernet_key_generator.py` can generate a new encryption key.

## Development and tests

Install dependencies from `requirements.txt` and run the test suite:

```bash
pip install -r requirements.txt
pytest
```

## Security notes

- API keys of VPN servers are stored encrypted in the database using Fernet.
- The admin API requires a login token obtained from the `/login` endpoint and should ideally be served over HTTPS.
- Login tokens are stored in Redis with a 1 hour TTL.
- Temporary configuration files created by the bot are placed in the system temp directory and removed immediately after sending.
- Communication with VPN servers is performed over plain HTTP; ensure your environment is trusted or switch to HTTPS.


## Deployment with Docker

A `docker-compose.yml` file is included to run the full stack with PostgreSQL.
Build the images and initialize the database first:

```bash
docker compose build
docker compose run --rm bot python scripts/init_db.py
```

Then start all services in the background:

```bash
docker compose up -d
```

Copy `.env.example` to `.env` and adjust the values (such as `BOT_TOKEN`,
`ENCRYPTION_KEY`, `POSTGRES_USER` and `POSTGRES_PASSWORD`) for your production
setup. Docker Compose will pick them up automatically and derive `DATABASE_URL`
from them. The admin API will be available on
port 5000.
