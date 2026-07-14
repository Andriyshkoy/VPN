# VPN Hub Admin API

The admin backend is a versioned, same-origin control plane under
`/api/admin/v1`. The React console is the primary client. PostgreSQL stores
administrator identities, revocable sessions, lockout state, actions and the
immutable audit trail; Redis is not part of admin authentication.

## Authentication and request safety

`POST /api/admin/v1/auth/login` accepts `username` and `password`, then sets a
`SameSite=Strict` HttpOnly session cookie plus a readable double-submit CSRF
cookie. Every `POST`, `PATCH` and `DELETE` must send the same CSRF value in
`X-CSRF-Token`. Mutations also enforce exact same-origin requests. CORS is
disabled because both production Nginx and the local Vite proxy are
same-origin.

The legacy `ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH` pair bootstraps the first
persisted owner on a successful login. Password failures are rate-limited and
persisted account lockout works across API workers. Session values and client
addresses are stored only as keyed/hash digests; logout can revoke either the
current session or all sessions.

Example with a local, non-secure cookie configuration:

```bash
curl -sS -c cookies.txt http://localhost:14081/api/admin/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me"}' >auth.json

CSRF=$(jq -r .csrf_token auth.json)
curl -sS -b cookies.txt http://localhost:14081/api/admin/v1/users

curl -sS -b cookies.txt \
  -X POST http://localhost:14081/api/admin/v1/users/1/balance-adjustments \
  -H "X-CSRF-Token: ${CSRF}" \
  -H 'Idempotency-Key: support-case-123' \
  -H 'Content-Type: application/json' \
  -d '{"direction":"credit","amount":"50.00","reason_code":"support_credit","comment":"Manual support credit","expected_balance":"0.00","expected_ledger_entry_id":null}'
```

Never put a session or CSRF token in a URL, browser storage, log or source
file. Production uses secure cookies and HTTPS.

## Roles

Permissions are checked by the API, not only hidden in the frontend:

| Role | Intended scope |
| --- | --- |
| `owner` | Every administrative permission |
| `support` | Users, referral context and config support; read-only servers |
| `finance` | Balances, ledger, payments, revenue and financial audit |
| `ops` | Config operations, server lifecycle, monitoring and audit |
| `viewer` | Read-only business and fleet overview |

Responses are permission-shaped: for example, dashboard finance or fleet data
is omitted when the actor lacks that permission, and the operations timeline
never unions server actions into a finance-only view.

## API areas

| Area | Paths and capability |
| --- | --- |
| Session | `/auth/login`, `/auth/me`, `/auth/logout`, `/auth/logout-all` |
| Dashboard | `/dashboard`, `/analytics/overview`, `/analytics/finance/timeseries` |
| User 360 | `/users`, `/users/{id}` plus paginated ledger, payments, configs, VPN operations, ancestry, children and rewards |
| Balance | `POST /users/{id}/balance-adjustments` with decimal strings, idempotency and optimistic balance/ledger guards |
| Finance | `/finance/ledger`, `/finance/payments`, `/finance/billing-runs`, `/finance/referral-rewards` |
| Referrals | `/referrals/tree` with bounded direction/depth traversal |
| Configs | `/configs`, `/configs/{id}`, `POST /configs/{id}/actions` |
| Fleet | `/servers`, status/history, lifecycle changes and `POST /servers/{id}/actions` |
| Operations | `/operations` unified, permission-filtered VPN/server action history |
| Operations data | `/observability/summary`, `/audit-events` |

All list endpoints are bounded and paginated. Search values are type-bounded
before reaching PostgreSQL. Monetary fields are serialized as decimal strings,
never JSON floating-point numbers. Remote Manager keys are write-only and
masked in every response.

## Fleet safety model

New servers always enter a quarantined `disabled` state and do not accept
placements. Endpoint or credential rotation re-quarantines the node and clears
its learned Manager identity. Activation requires an explicit action after a
fresh authenticated Manager status sample proves readiness, data-plane health
and stable instance identity.

Drain and retirement are guarded by desired and observed config state. A
config remains managed until revocation is observed remotely, so a failed or
pending revoke cannot make a destructive server change appear safe. Server
actions use idempotency keys and version guards and are recorded independently
from immutable audit events.

## Compatibility and observability

The old `/login` and unversioned Bearer API are registered only when
`ADMIN_LEGACY_API_ENABLED=true`; it is `false` by default and intended solely
as a short rollback switch. Internal `/health`, `/ready` and `/metrics` are not
browser admin endpoints. Production Nginx exposes only the versioned admin API
and SPA routes.

Request and correlation IDs are validated, returned as `X-Request-ID` and
`X-Correlation-ID`, and attached to audit/action records. Audit rows are
append-only at the database layer.
