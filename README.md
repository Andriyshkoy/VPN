# VPN Service

This project implements a small VPN management system consisting of a Telegram bot, a web based admin panel and a billing daemon. It can manage multiple VPN servers via a REST API, generate client configuration files and handle basic billing based on the number of active configs.

## Technology stack

- **Python 3** with `asyncio`
- [**Aiogram**](https://github.com/aiogram/aiogram) for the Telegram bot
- [**Flask**](https://flask.palletsprojects.com/) for the admin web panel
- [**SQLAlchemy**](https://www.sqlalchemy.org/) (async) as ORM
- [**Pydantic**](https://docs.pydantic.dev/) for settings and service models
- [**Cryptography**](https://cryptography.io/) (Fernet) to store server API keys encrypted
- [**httpx**](https://www.python-httpx.org/) for talking to remote VPN servers
- `pytest` with `pytest-asyncio` for the tests

## Components

### Telegram bot

Located in [`bot/`](bot). It allows users to register, view balance and create VPN configs. A simple FSM guides the user through choosing a server and entering a display name. Config files are sent as temporary files and removed afterwards.

### Admin panel

Located in [`admin/`](admin). It exposes a minimal HTML interface to manage servers, users and configs. Authentication is provided via HTTP basic auth if `ADMIN_PASSWORD` is set. Start it with:

```bash
python -m admin.app
```

and open `http://localhost:14081`.

### Billing daemon

`scripts/billing_daemon.py` periodically charges users for their active configs and suspends them when the balance goes negative. Run it as a standalone process.

### Database

Async SQLAlchemy models live under [`core/db`](core/db). Use `scripts/init_db.py` once to create the schema:

```bash
python scripts/init_db.py
```

## Configuration

All settings are read from environment variables (see `core/config.py`).
Create a `.env` file (see `.env.example`) to provide them:

- `DATABASE_URL` – database connection string
- `ENCRYPTION_KEY` – Fernet key used to encrypt server API keys
- `BOT_TOKEN` – Telegram bot token
- `PER_CONFIG_COST` – how much to charge per active config (default `1.0`)
- `BILLING_INTERVAL` – seconds between periodic charges
- `ADMIN_PASSWORD` – password for the admin panel (leave empty to disable auth)

A helper script `scripts/fernet_key_generator.py` can generate a new encryption key.

## Development and tests

Install dependencies from `requirements.txt` and run the test suite:

```bash
pip install -r requirements.txt
pytest
```

## Security notes

- API keys are stored encrypted in the database using Fernet.
- The admin panel should be protected with a strong `ADMIN_PASSWORD` and ideally served over HTTPS.
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
`ENCRYPTION_KEY` and `ADMIN_PASSWORD`) for your production setup. Docker
Compose will pick them up automatically. The admin panel will be available on
port 14081 (forwarded to 5000 in the container).
