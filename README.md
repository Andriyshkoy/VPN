# VPN Service

This project implements a small VPN management system consisting of a Telegram bot, a simple admin API and a billing daemon. It can manage multiple VPN servers via a REST API, generate client configuration files and handle basic billing based on the number of active configs.

## Technology stack

- **Python 3.12** with `asyncio`
- [**FastAPI**](https://fastapi.tiangolo.com/) for the admin API
- [**Aiogram**](https://github.com/aiogram/aiogram) for the Telegram bot
- [**SQLAlchemy**](https://www.sqlalchemy.org/) (async) with [**Alembic**](https://alembic.sqlalchemy.org/) for migrations
- [**Pydantic**](https://docs.pydantic.dev/) for settings and API schemas
- [**PostgreSQL**](https://www.postgresql.org/) as the database
- [**Redis**](https://redis.io/) with [**RQ**](https://python-rq.org/) for background tasks
- [**httpx**](https://www.python-httpx.org/) for talking to VPN servers
- [**React**](https://react.dev/) + [**Vite**](https://vitejs.dev/) for the admin UI
- [**Nginx**](https://nginx.org/) as a reverse proxy
- [**Cryptography**](https://cryptography.io/) (Fernet) to store server API keys encrypted
- `pytest` with `pytest-asyncio` for the tests
- Docker and Docker Compose for deployment

## Features

- Manage multiple VPN servers through a unified REST API
- Telegram bot for users to register, pay via Telegram Pay and generate config files
- Web-based admin panel with React for managing servers, users and configs
- Billing daemon that charges users via Redis/RQ tasks, sends low-balance notifications and suspends unpaid configs
- Config files are generated on demand and removed after sending
- Server API keys are stored encrypted using Fernet
- Nginx reverse proxy exposes the admin API and frontend on a single port

## Components

### Telegram bot

Located in [`bot/`](bot). It allows users to register, view balance and create VPN configs. A simple FSM guides the user through choosing a server and entering a display name. Config files are sent as temporary files and removed afterwards.

### Admin API

Located in [`admin/`](admin). It exposes a JSON REST API to manage servers, users and configs. Endpoints are protected either by an `X-API-Key` or by a login token obtained from `/login`. When running with Docker Compose the service listens on port 8000 and is proxied through Nginx at `http://localhost:14081/api`. Request bodies are validated with Pydantic models.

### Admin frontend

The React + Vite application in [`admin_frontend/`](admin_frontend) provides a web interface for the admin API. It is served through the Nginx container together with the API endpoints.

### Billing daemon

`billing_daemon/main.py` uses Redis and RQ to periodically charge users for their active configs. It also sends Telegram notifications when a user's balance falls below 10 and suspends configs when the balance becomes negative. The worker and scheduler are provided as separate Docker services.

### Database

Async SQLAlchemy models live under [`core/db`](core/db). Initialize the schema using the bot container:

```bash
docker compose run --rm bot python scripts/init_db.py
```

## Configuration

All settings are read from environment variables (see `core/config.py`).
Create a `.env` file (see `.env.example`) to provide them. Docker Compose
derives `DATABASE_URL` automatically from the PostgreSQL credentials:

- `DATABASE_URL` – database connection string
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
from them. The Nginx container exposes the stack on
`http://localhost:14081` with the admin API available under `/api`.
