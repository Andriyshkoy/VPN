# <p align="center"><img src="https://img.shields.io/badge/andriyshkoy%20VPN-FF6F00?style=for-the-badge&logo=openvpn&logoColor=white" alt="andriyshkoy VPN"/></p>

> **A sleek, fully‑automated VPN management stack — now with extra flair.**

---

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" />
  <img src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white" />
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
</p>

# VPN Service

This project implements a small VPN management system consisting of a Telegram bot and a billing daemon. It can manage multiple VPN servers via their APIs, generate client configuration files and handle basic billing based on the number of active configs.

## Technology stack 🧰

<table>
  <tr>
    <td><a href="https://www.python.org/"><img src="https://cdn.jsdelivr.net/gh/devicons/devicon/icons/python/python-original.svg" width="40"/></a></td>
    <td><a href="https://github.com/aiogram/aiogram"><img src="https://avatars.githubusercontent.com/u/45650664?s=200&v=4" width="40"/></a></td>
    <td><a href="https://www.sqlalchemy.org/"><img src="https://www.sqlalchemy.org/img/sqla_logo.png" width="40"/></a></td>
    <td><a href="https://alembic.sqlalchemy.org/"><img src="https://alembic.sqlalchemy.org/en/latest/_static/alembic_logo.png" width="40"/></a></td>
    <td><a href="https://pydantic.dev/"><img src="https://avatars.githubusercontent.com/u/50623616?s=200&v=4" width="40"/></a></td>
    <td><a href="https://www.postgresql.org/"><img src="https://cdn.jsdelivr.net/gh/devicons/devicon/icons/postgresql/postgresql-original.svg" width="40"/></a></td>
  </tr>
  <tr>
    <td><a href="https://redis.io/"><img src="https://cdn.jsdelivr.net/gh/devicons/devicon/icons/redis/redis-original.svg" width="40"/></a></td>
    <td><a href="https://python-rq.org/"><img src="https://avatars.githubusercontent.com/u/1177768?s=200&v=4" width="40"/></a></td>
    <td><a href="https://www.python-httpx.org/"><img src="https://avatars.githubusercontent.com/u/67855638?s=200&v=4" width="40"/></a></td>
    <td><a href="https://cryptography.io/"><img src="https://avatars.githubusercontent.com/u/1728152?s=200&v=4" width="40"/></a></td>
  </tr>
</table>

* **Python 3.12** with `asyncio`
* [**Aiogram**](https://github.com/aiogram/aiogram) for the Telegram bot
* [**SQLAlchemy**](https://www.sqlalchemy.org/) (async) with [**Alembic**](https://alembic.sqlalchemy.org/) for migrations
* [**Pydantic**](https://docs.pydantic.dev/) for settings and API schemas
* [**PostgreSQL**](https://www.postgresql.org/) as the database
* [**Redis**](https://redis.io/) with [**RQ**](https://python-rq.org/) for background tasks
* [**httpx**](https://www.python-httpx.org/) for talking to VPN servers
* [**Cryptography**](https://cryptography.io/) (Fernet) to store server API keys encrypted
* `pytest` with `pytest-asyncio` for the tests
* Docker and Docker Compose for deployment

## Features ✨

* 🌐 Manage multiple VPN servers through their APIs
* 🤖 Telegram bot for users to register, pay via Telegram Pay and generate config files
* 💸 Billing daemon that charges users via Redis/RQ tasks, sends low-balance notifications and suspends unpaid configs
* 🧾 Balance ledger with transaction history and runtime-adjustable billing rates
* 🎁 Referral bonuses on deposits with ledger tracking
* 📄 Config files are generated on demand and removed after sending
* 🔐 Server API keys are stored encrypted using Fernet

## Components 🧩

### Telegram bot

Located in [`bot/`](bot). It allows users to register, view balance and create VPN configs. A simple FSM guides the user through choosing a server and entering a display name. Config files are sent as temporary files and removed afterwards. Admins can manage users, servers and billing settings from the same bot.

### Billing daemon

`billing_daemon/main.py` uses Redis and RQ to periodically charge users for their active configs. It also sends Telegram notifications when a user's balance falls below 10 and suspends configs when the balance becomes negative. The worker and scheduler are provided as separate Docker services.

### Database

Async SQLAlchemy models live under [`core/db`](core/db). Initialize the schema using the bot container:

```bash
docker compose run --rm bot python scripts/init_db.py
```

## Configuration ⚙️

All settings are read from environment variables (see `core/config.py`).
Create a `.env` file (see `.env.example`) to provide them. Docker Compose
derives `DATABASE_URL` automatically from the PostgreSQL credentials:

* `DATABASE_URL` – database connection string
* `ENCRYPTION_KEY` – Fernet key used to encrypt server API keys
* `BOT_TOKEN` – Telegram bot token
* `BILLING_INTERVAL` – seconds between periodic billing runs
* `ADMIN_TG_IDS` – comma-separated Telegram IDs with admin access in the bot
* `REDIS_URL` – Redis connection string for token storage (default `redis://redis:6379/0`)

Billing rates (config creation fee, monthly usage rate, referral bonuses) are stored
in the database and can be updated at runtime via the bot admin menu.

A helper script `scripts/fernet_key_generator.py` can generate a new encryption key.

## Development and tests 🧪

Install dependencies from `requirements.txt` and run the test suite:

```bash
pip install -r requirements.txt
pytest
```

## Security notes 🔐

* API keys of VPN servers are stored encrypted in the database using Fernet.
* Temporary configuration files created by the bot are placed in the system temp directory and removed immediately after sending.
* Communication with VPN servers is performed over plain HTTP; ensure your environment is trusted or switch to HTTPS.

## Deployment with Docker 🐳

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
from them.

---

<p align="center">Made with ❤️ by <a href="https://github.com/andriyshkoy">andriyshkoy</a></p>
