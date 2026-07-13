# Admin Frontend

This is a small React application for interacting with the VPN Admin API.

## Setup

Install dependencies:

```bash
npm install
```

For standalone Vite development, create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
# edit .env if the Nginx/API origin differs
```

Environment variables used by the app:

- `VITE_ADMIN_API_URL` – browser-visible Nginx/API origin. The example uses
  `http://localhost:14081`, matching the root Docker Compose stack.

The root Compose file injects this variable directly, so
`admin_frontend/.env` is not required when the frontend runs in Compose.

## Development

Start the dev server with:

```bash
npm run dev
```

Then open the shown URL in the browser and log in using the credentials configured on the server via the `/login` endpoint.
