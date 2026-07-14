# VPN Hub admin console

Production-oriented React and TypeScript SPA for `/api/admin/v1`.

## Local development

```bash
npm ci
npm run dev
```

Vite proxies `/api` to `http://localhost:8000` by default. Override the proxy
only for local development:

```bash
ADMIN_API_PROXY=http://localhost:14081 npm run dev
```

The browser always uses same-origin `/api/admin/v1`; there is no build-time API
origin or browser-visible credential.

## Checks

```bash
npm run lint
npm test
npm run build
```

Vitest, React Testing Library and MSW cover session authentication, users,
balance adjustments with optimistic guards, and protected route smoke tests.

## Security model

- Authentication uses the server-issued HttpOnly session cookie.
- Every request sends `credentials: "include"`.
- Mutations send the CSRF value from the `vpn_admin_csrf` cookie or the latest
  auth response as `X-CSRF-Token`.
- No credentials or session markers are stored in local storage.
- Money is accepted and submitted as decimal strings, never floating-point
  JSON numbers.
- Balance and server actions use idempotency keys; balance writes also submit
  the current balance and ledger snapshot, and server writes submit the server
  version when required.

The API adaptation layer lives in `src/api`. Backend wire formats are converted
there into stable UI models, keeping nested users/configs and operational
payload changes out of page components.

## Container

The Dockerfile builds hashed static assets in a Node stage and serves them from
an unprivileged Nginx process on port `5173`. Browser routes fall back to
`index.html`; immutable assets receive a one-year cache policy.
