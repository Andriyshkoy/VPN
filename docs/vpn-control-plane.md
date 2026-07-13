# VPN Manager transport and drift reconciliation

The Hub keeps legacy Manager HTTP behaviour by default. HTTPS, mutual TLS,
inventory audit and repair are backend capabilities and do not change the bot
or admin user interfaces.

## HTTPS and mutual TLS

Set the following only after OpenVPN Manager exposes its parallel TLS listener:

```dotenv
VPN_MANAGER_TLS_ENABLED=true
VPN_MANAGER_MTLS_REQUIRED=true
VPN_MANAGER_TLS_PORT=16291
VPN_MANAGER_CA_CERT_PATH=/run/secrets/vpn-manager/ca.crt
VPN_MANAGER_CLIENT_CERT_PATH=/run/secrets/vpn-manager/client.crt
VPN_MANAGER_CLIENT_KEY_PATH=/run/secrets/vpn-manager/client.key
```

Local Compose mounts `${VPN_MANAGER_TLS_DIR:-./secrets/vpn-manager}` and
production separately mounts
`${VPN_MANAGER_TLS_DIR_PROD:-/etc/vpn-hub/manager-pki}` read-only at
`/run/secrets/vpn-manager` in the admin, bot and RQ worker containers. Separate
variables prevent a copied local environment from redirecting the production
bind to a relative path. Local secret contents are ignored by Git and excluded
from Docker build contexts. Store these files in the selected host directory:

The application settings retain legacy-compatible defaults for development,
but `docker-compose-prod.yml` overrides TLS, mTLS, and port `16291` to fail
closed. A production release cannot report ready while silently falling back
to the legacy HTTP listener.

```text
ca.crt       CA used to verify the Manager server certificate
client.crt   Hub workload certificate accepted by Manager nginx
client.key   private key for client.crt
```

Backend images use the fixed non-root UID/GID `10001:10001`. On a Linux Docker
host, keep the directory and private key inaccessible to unrelated users while
granting the container group read access, for example:

```bash
chown root:10001 /etc/vpn-hub/manager-pki
chmod 0750 /etc/vpn-hub/manager-pki
chown root:10001 /etc/vpn-hub/manager-pki/client.key
chmod 0640 /etc/vpn-hub/manager-pki/client.key
chmod 0644 /etc/vpn-hub/manager-pki/ca.crt \
  /etc/vpn-hub/manager-pki/client.crt
```

Use numeric IDs deliberately: they are evaluated by the kernel for bind
mounts, regardless of whether the host has a group named `10001`. Verify with
the exact release image before rollout. To use ordinary server-side HTTPS
without mTLS, leave both client paths empty. To use a publicly trusted server
certificate, the CA path may also be empty and the system trust store is used.
Certificate verification and hostname/IP SAN checks are never disabled.

With `VPN_MANAGER_MTLS_REQUIRED=true`, settings validation requires TLS plus
non-empty CA, client-certificate and client-key paths before the process starts.
The mounted files themselves are opened lazily by `APIGateway`, because Docker
secret mounts are unavailable while settings are parsed. Missing, unreadable or
temporarily rotated TLS material raises a retryable `APITLSConfigurationError`:
the durable VPN operation remains pending/failed for normal backoff and does
not become a definitive rejection or trigger a provisioning refund.

`VPN_MANAGER_TLS_PORT` overrides the legacy port stored on existing Server
rows only while TLS is enabled. This allows the Manager's HTTP `:16290` and
mTLS `:16291` listeners to overlap without modifying rows that already own VPN
configs.

Safe rollout order:

1. Enable and validate the Manager's parallel mTLS listener while legacy HTTP
   remains live.
2. Mount the Hub CA/client identity with TLS still disabled.
3. From the same network namespace, verify the Manager certificate SAN and a
   client-authenticated request.
4. Set `VPN_MANAGER_TLS_ENABLED=true` and recreate Hub backend containers.
5. Audit every Manager inventory and observe transport errors before retiring
   the legacy listener.

Never replace the OpenVPN data-plane CA, `tls-crypt` key or issued client
profiles during this control-plane migration.

## Typed Manager inventory

OpenVPN Manager 1.2 adds:

```text
GET /clients
GET /clients/{name}/state
```

`APIGateway.get_client_inventory()` validates the complete response and returns
typed `ManagerClientInventory`/`ManagerClientState` values. Passing the prior
ETag sends `If-None-Match`; a Manager `304` returns `None`. Invalid state names,
artifact flags, counts or JSON fail as `APIProtocolError` rather than being
treated as trustworthy lifecycle state.

The existing create/download/suspend/unsuspend/revoke routes and legacy HTTP
transport remain unchanged when the new settings are disabled.

## Read-only drift audit

`VPNDriftService.audit_server(server_id, etag=...)` compares Manager inventory
with Hub desired/actual state. Audit performs no database writes and never
calls a mutation endpoint. Findings distinguish:

- active/suspended state mismatch;
- stale Hub actual state;
- missing or inconsistent Manager PKI/profile state;
- live clients known only to Manager;
- inert revoked/expired Manager history.

The ETag describes Manager inventory only, not Hub desired state. If Manager
answers `304`, the audit records that the remote side was unchanged but fetches
the full inventory once more before comparing it with freshly loaded Hub rows;
it never turns a remote-only cache hit into an empty drift report.

Unknown remote clients are intentionally reported, never automatically
revoked. Missing, expired, orphaned, incomplete, provisioning, failed and
revoking records also require operator review.

## Explicit safe repair

Repair is disabled by default:

```dotenv
VPN_DRIFT_REPAIR_ENABLED=false
```

Enabling the setting does not schedule repair. A backend caller must first
audit, retain the exact inventory `revision`, select explicit Hub config IDs,
and call:

```python
report = await drift.audit_server(server_id)
result = await drift.repair_server(
    server_id,
    expected_revision=report.inventory_revision,
    config_ids=[selected_config_id],
)
```

The service fetches inventory again and rejects a changed revision. It then
re-locks every selected config and verifies the desired state has not changed.
Only ACTIVE/SUSPENDED convergence is eligible. All intents are committed as
existing leased/fenced `vpn_operation` rows before Manager calls begin, so a
crash is recovered by the normal durable operation reconciler. Maintenance
mode disables repair, and activation also respects `PROVISIONING_ENABLED`.

There is deliberately no automatic create or revoke repair: those actions can
invalidate client access or remove an unknown credential and require a separate
operator decision.
