# Hub integration with Hysteria and REALITY

The Hub now supports an explicit `node-agent` backend alongside the unchanged
`openvpn-manager` backend. This integration adds technical access management;
customer products, prices, bot menus and payment policy are separate work.

## Persistent state

Migration `g5c9d2e3f4a5` follows the audited production revision
`f4b8c1d2e3a4`. Existing servers default to `openvpn-manager`; existing configs,
balances and ledger rows are retained. New server fields are `backend_type`,
`node_id` and `endpoint_revision`. A changed endpoint, credential, backend or
node identity increments the endpoint revision, invalidating old readiness.

A new config carries `service_id` (`hysteria` or `vless-reality`) and a
JSON-compatible technical policy. Hysteria accepts `activation_days`; VLESS
accepts an aware `expires_at`. Quotas count bytes; zero means unlimited. The
normalized creation policy is persisted before network calls and never
recalculated from the retry time.

`vpn_access_binding` retains node/service identity, the original provision
operation UUID, policy, returned remote access UUID and observed state. Revoke
keeps this mapping as a tombstone after the old `vpn_config` row is deleted.
The downgrade refuses to discard nonempty binding history.

## Operations and recovery

`ConfigService` preserves the existing durable operations, leases and fencing.
Its node path uses a separate `NodeAgentGateway`; legacy OpenVPN requests and
patch points remain unchanged. The Hub operation UUID is the node
`Idempotency-Key`. `owner_ref=hub:config:<id>` supports reconciliation without
embedding Telegram identities.

After a timeout or a crash before completion, the worker replays the original
provision key and body to recover the same remote UUID. The UUID is attached
atomically with operation completion under the current fencing token. A stale
worker cannot overwrite the binding. Cleanup after an exhausted ambiguous
provision also recovers its original identity before revoking it.

Node authentication/configuration failures remain retryable administrative
problems. A later rejection after an ambiguous attempt is not treated as proof
that no remote access existed. Unknown access bindings are never adopted or
deleted automatically.

A node provision only completes when the control plane is verified and no
operation is pending. `failed`, `provisioning` and already-revoked provision
results remain unresolved. A real `expired` or `limited` access retains that
observed state; it does not become active or financial suspension. Technical
operation completion alone is not evidence for charging or client traffic.

## Operator API

New permissions `configs:provision` and `configs:export` belong to OWNER and
OPS. Ordinary config read/write roles do not gain secret export or technical
issuance.

- `POST /api/admin/v1/configs` accepts a UUID `Idempotency-Key`, owner/server IDs,
  internal/display names, service, policy and an audit reason. It invokes the
  technical ConfigService directly without BillingService or ledger mutation.
  Completed operations return 201, queued operations 202, terminal failures 502.
- `GET /api/admin/v1/configs/{id}/profile` explicitly exports a typed URI with
  `Cache-Control: no-store`. Audit and ordinary metadata contain no URI.
- Existing suspend/unsuspend/revoke actions dispatch to the selected backend.
  `download_config()` remains the legacy OpenVPN bytes API; a node access must
  use explicit URI export.

Existing maintenance/provisioning gates still apply. This endpoint is not a
customer purchase flow. Production autocharges and payment/provisioning gates
must retain their reviewed values. Accept creation on isolated staging with a
test user and zero-cost test settings, without polling the real Telegram bot.
The historical `promote_full_production.sh` explicitly enables billing and
payments in its own promotion scenario; do not run it as an incidental IaC
update while the product remains paused.

Legacy `ServerService.list(available_only=True)` remains OpenVPN-only; future
node callers opt in with `backend_type="node-agent"`. Current bot menus and
`.ovpn` download flows therefore cannot accidentally select a node server.

## Management transport

Remote nodes default to verified HTTPS. Dedicated optional settings
`NODE_AGENT_CA_CERT_PATH`, `NODE_AGENT_CLIENT_CERT_PATH` and
`NODE_AGENT_CLIENT_KEY_PATH` configure private trust and client identity;
client certificate/key must be configured together. They do not reuse the
OpenVPN Manager TLS settings.

The current deployment can select the encrypted WireGuard management network
through `NODE_AGENT_ENDPOINTS_FILE`. The private read-only JSON catalog has
this shape; substitute a real opaque token outside Git:

```json
{"nl-1":{"base_url":"http://10.77.78.2:18010","token":"<operator token>","transport":"wireguard-http"}}
```

The file must be regular, not a symlink, mode 0400 or 0600, owned by root or the
reader. Hub containers run as UID 10001, so a mounted catalog must be readable
by that UID; root-owned 0600 is not readable by the app. The resolver checks the
exact node ID, canonical literal IP/port and token against the database before
allowing HTTP. A missing entry, changed endpoint/token, unsafe permissions or
invalid catalog fails before network access. There is no blanket private-IP
exception. Proxy environment variables and redirects are disabled.

HTTPS trust material is omitted for explicitly selected WireGuard HTTP nodes,
so future HTTPS nodes can share the same Hub without breaking the existing
WireGuard path.

## Health and acceptance

Node readiness is stored as control-plane readiness, with `data_plane=unknown`
and no fabricated OpenVPN PKI/Manager identity. Placement and activation require
fresh successful readiness for the exact node and endpoint revision. OpenVPN
inventory/drift/repair actions reject node servers explicitly.

Coverage includes unchanged OpenVPN lifecycle, exact idempotent replays,
timeout/crash recovery, stale-worker fencing, durable revoke tombstones,
limited/expired states, scoped URI export, truthful health and management
transport catalog validation. PostgreSQL migration and concurrency validation
must also run in CI or isolated staging; SQLite is not a substitute for those
checks. Native node traffic acceptance remains separate from Hub readiness.

Code-only rollback after registering node servers requires stopping old
provisioner/reconciler processes or keeping node servers unavailable: old code
does not understand backend types. Preserve the additive database and binding
history during rollback.
