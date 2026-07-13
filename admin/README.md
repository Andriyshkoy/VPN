# VPN Admin API

The FastAPI application is exposed through Nginx at `/api`; login is available
at `/login`. Obtain a token with `POST /login` and send it on protected requests:

```text
Authorization: Bearer <token>
```

Tokens are stored in Redis with a one-hour TTL.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/login` | Authenticate and return `{"token": "..."}` |
| `GET` | `/api/servers` | List servers (`limit`, `offset`, `host`, `location`) |
| `POST` | `/api/servers` | Create a server |
| `GET` | `/api/servers/{server_id}` | Get a server |
| `PATCH` | `/api/servers/{server_id}` | Update supplied server fields |
| `DELETE` | `/api/servers/{server_id}` | Delete a drained server |
| `GET` | `/api/users` | List users (`limit`, `offset`, `username`, `tg_id`) |
| `POST` | `/api/users` | Create/register a user |
| `GET` | `/api/users/{user_id}` | Get a user |
| `PATCH` | `/api/users/{user_id}` | Update supplied user fields |
| `DELETE` | `/api/users/{user_id}` | Delete a user without configs or financial history |
| `POST` | `/api/users/{user_id}/topup` | Add a positive amount through the ledger |
| `POST` | `/api/users/{user_id}/withdraw` | Withdraw a positive amount through the ledger |
| `GET` | `/api/configs` | List configs (`limit`, `offset`, `server_id`, `owner_id`, `suspended`) |
| `GET` | `/api/configs/{config_id}` | Get a config |

Deletion endpoints keep the response shape `{"deleted": true|false}`. They
return `false` when deletion would discard managed VPN credentials or immutable
financial history.

`topup` and `withdraw` accept an optional `Idempotency-Key` header. Reuse the
same value when retrying one administrative balance operation. Attempts to
change a Manager IP, port, or API key while that server still owns configs are
rejected with HTTP 409 until the server is drained.

Server responses keep the `api_key` field for frontend compatibility, but its
value is always `********`; decrypted Manager credentials are never returned.

## Examples

```bash
TOKEN=$(curl -sS http://localhost:14081/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me"}' | jq -r .token)

curl -sS http://localhost:14081/api/users \
  -H "Authorization: Bearer $TOKEN"

curl -sS http://localhost:14081/api/users/1/topup \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"amount":100}'
```
