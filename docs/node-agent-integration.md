# Hub integration with Hysteria and REALITY

This is a design for the next additive integration. The imported node-agent does
not change Hub schemas, financial policy, bot handlers or the OpenVPN adapter.

## Existing extension points

| Location | Required change |
| --- | --- |
| `core/services/api_gateway.py` | Keep the OpenVPN `X-API-Key`, name and `.ovpn` contract; add a separate `NodeAgentGateway` for Bearer/access UUID/URI semantics |
| `core/services/config.py::_create_gateway` | Choose the backend from explicit server metadata, preserving legacy callers and patch points |
| `core/services/_config_shared.py::_ConfigContext` | Carry backend type, service ID, node ID and durable remote access identity |
| `core/services/config_executor.py::_execute` | Persist the returned node access ID under the existing lease/fencing token |
| `core/services/config_provisioning.py::prepare_config` | Persist immutable service and technical policy before any network request |
| `core/services/config_queries.py::download_config` | Return a typed URI export for the new backend while retaining `.ovpn` for legacy configs |
| `core/services/fleet_placement.py` | Add node-agent control-plane health without inventing OpenVPN PKI or client data-plane health |

## Data and operation contract

Use an additive migration. `server.backend_type` defaults to `openvpn-manager`
for existing rows. New node servers carry explicit node ID and API version;
new access intents carry `service_id` (`hysteria` or `vless-reality`).

Persist a `vpn_access_binding` or equivalent durable mapping from the Hub config
to the returned node `access.id`, unique per server/remote ID. Preserve this
mapping as a tombstone after revoke: the current executor deletes `vpn_config`
on successful revoke, and discarding the remote identity would obstruct later
reconciliation and restore.

Use the existing persisted Hub operation UUID as `Idempotency-Key`. Save the
exact method, route and technical policy in `vpn_operation.payload`; retries
must not recalculate an expiry from the current time or select another node.
`owner_ref=hub:config:<id>` helps reconciliation but is not a replacement for
`remote_access_id` or tenant authorization.

The current executor ignores the result of `create_client`. The new adapter
must persist the returned access ID atomically with operation completion under
the fencing token. A worker that loses its lease cannot commit a binding.
After timeout, replay the same operation and reconcile metadata; never issue a
second identity merely because a profile download failed.

The old ambiguous-create fallback downloads an `.ovpn` file. That cannot be
copied literally: the new profile endpoint can return `409` for suspended,
limited or expired accesses. Provisioning confirmation and secret export are
separate operations. Keep URI secrets out of operation payloads, errors, fleet
snapshots and normal list responses.

Node states `expired` and `limited` need an explicit observed state/reason;
they must not silently become `active` or financial suspension. Hysteria
`activation_days` and VLESS `expires_at` have different precision. Subscription
periods remain central product data. Delayed counters marked
`billing_authoritative=false` must not drive new byte-based money deductions.

## Acceptance before enabling the products

- Existing OpenVPN rows retain the previous adapter and behaviour.
- Timeout after remote create and crash before local binding both recover one
  remote UUID, without duplicate access or monetary mutation.
- Stale leases cannot overwrite bindings; suspend/resume preserve the profile.
- Revoke keeps a durable binding tombstone and denies fresh connections.
- Node `/readyz` is presented as control-plane readiness, not working user traffic.
- Additive migrations on a PostgreSQL copy preserve configs, balances and ledger.

Build on the existing lifecycle, operation lease, gateway and fleet-placement
test suites. Prices, renewal rules, payment channels and activation of billing
remain explicit product decisions; importing the node agent does not set them.
