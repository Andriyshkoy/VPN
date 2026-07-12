# Admin Frontend

This is a small React application for interacting with the VPN Admin API.

## Setup

Install dependencies:

```bash
npm install
```

Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
# edit .env and specify the API URL
```

Environment variables used by the app:

- `VITE_ADMIN_API_URL` – base URL of the Admin API

## Development

Start the dev server with:

```bash
npm run dev
```

Then open the shown URL in the browser and log in using the credentials configured on the server via the `/login` endpoint.
