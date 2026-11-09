# Native VPN node agent

The platform repository now includes `apps/node-agent`, imported from the tested
node-agent source commit `45f9dcdcce13363d9953247267b53491b74cc9a9`.
Its runtime dependencies and tests are separate from the Hub environment.
The shared contract lives at `contracts/node-agent-v1/openapi.json`.

The production implementation uses native systemd services: a non-root API and
a narrowly scoped local Hysteria bridge. The API remains on loopback. Central
accounts, PostgreSQL, balances, payments and the Telegram bot are not installed
by the node-agent package. Infrastructure inventory, secrets, management
networking and deployment orchestration belong in the private `vpn-infra`
repository.

## Build and verify

```sh
cd apps/node-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/python -m pytest -q tests deploy/test_backup.py
.venv/bin/python scripts/check_contract.py
.venv/bin/python scripts/build_release.py
```

The build uses the installer's explicit source allowlist and deterministic tar
metadata. It writes a native archive and `dist/release.json`, recording its SHA,
the platform Git revision, dirty-source state, lockfile and OpenAPI checksums.
CI builds this artifact after node-specific tests and contract validation.
It does not publish profiles, tokens, databases, private evidence or credentials.

`releases/node-agent.json` describes the code component and imported revision.
The deployment inventory chooses an exact platform commit and archive SHA;
the same artifact is promoted between staging and production. Runtime
dependencies must be provisioned by IaC or made available from an approved
package mirror/wheel archive. A source archive alone is not a complete offline
environment.

## Deployment boundary

The existing `deploy/install.py` is a transition installer for the already
prepared NL host. Its dry run only packages source; `--execute` targets the
configured NL SSH destination and must not be used as a generic fleet installer.
IaC should orchestrate the bootstrap using inventory-specific settings.
No server is changed by building or downloading this artifact.

An existing node's bearer credentials must be adopted from the protected
deployment records. A new checkout has no `private/` directory; running the
transition installer there would generate new plaintext tokens while preserving
the existing server-side hash catalogue. This is not token rotation and those
new tokens would not authenticate. IaC must supply the matching existing
credentials through its encrypted secret delivery, or perform a separately
implemented explicit rotation.

Hysteria currently depends on a legacy MongoDB/auth/scheduler stack and REALITY
on Marzban. Their state, credentials, TLS/REALITY material and pinned dependencies
must be adopted or restored separately. `clean_host_installer=false` in the
manifest is intentional; agent readiness alone does not prove a clean-host
rebuild. See [backend dependencies](../apps/node-agent/deploy/backend-dependencies.json)
and [the node runbook](../apps/node-agent/deploy/OPERATIONS.md).

The node-agent owns only accounts it created. Existing Hysteria and VLESS keys
are not automatically imported. Suspend/update deny new authentication;
termination of already-open streams is not guaranteed. Hysteria activation and
usage are delayed observations, not financial accounting clocks.

Local test outcomes and live acceptance limitations are documented in
[the acceptance guide](../apps/node-agent/acceptance/README.md). The central
adapter and additive binding migration are described in
[the integration guide](node-agent-integration.md). Apply that migration and
configure the protected management endpoint before enabling central issuance.
The bot's product interface and financial policy are a separate rollout.
