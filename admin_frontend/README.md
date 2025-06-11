# Admin Frontend

This is a small React application for interacting with the VPN Admin API.

## Setup

Install dependencies:

```bash
npm install
```

Create a `.env` file based on `.env.example` and set your credentials:

```bash
cp .env.example .env
# edit .env and specify your login, password and API settings
```

Environment variables used by the app:

- `VITE_ADMIN_USERNAME` – login for the web interface
- `VITE_ADMIN_PASSWORD` – password for the web interface
- `VITE_ADMIN_API_URL` – base URL of the Admin API
- `VITE_ADMIN_API_KEY` – API key sent in `X-API-Key` header

## Development

Start the dev server with:

```bash
npm run dev
```

Then open the shown URL in the browser and log in with the credentials from your `.env` file.
