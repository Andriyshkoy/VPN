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
balance adjustments with optimistic guards, subscription and infinite-balance controls, reviewed campaigns, and protected route smoke tests.

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

## Prepared subscription and campaign workflows

- `/subscriptions` shows account-level hourly rates: Basic 0.07 RUB/hour and
  Plus 0.14 RUB/hour. About 50/100 RUB per 30 days are orientation prices;
  720 hours cost 50.40/100.80 RUB. This is not monthly prepayment.
- Basic includes Hysteria 2 with one concurrent client session, without a
  hardware binding. Plus includes Hysteria 2 and VLESS/REALITY with unlimited
  devices. Profile issue/reissue is free.
- User detail has a subscription tab. Plan changes use an optimistic version
  guard, audit reason and stable idempotency key. They do not charge the wallet
  or turn on the global billing switch.
- Infinite balance is only an explicit admin action with a reason and
  confirmation. The finite wallet and ledger are preserved; disabling the flag
  restores ordinary hourly billing whenever global billing is enabled. No user
  is automatically granted the flag by this frontend.
- Migration preview only reads the proposed mapping (one active profile to
  Basic, two or more to Plus). Fully suspended accounts remain off. There is no
  migration-apply control here.
- `/campaigns` supports drafts and selected users, a reviewed audience snapshot,
  an explicit launch confirmation, delivery progress, cancellation and retry of
  known transient failures. Unknown delivery outcomes are not retried.
  When the API reports delivery paused, the UI states that launch only creates
  a waiting queue; it never enables delivery. The same explicit confirmation
  remains required.

These changes are prepared for review. Building or testing this frontend does
not deploy it, enable billing, apply a migration, or send Telegram messages.
All network mutations remain subject to server RBAC, session CSRF and audits.
