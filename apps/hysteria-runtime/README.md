# Hysteria compatibility runtime

Standalone authentication and delayed traffic collection for **new isolated
nodes and staging**. Existing production Blitz authentication, scheduler and
MongoDB are not replaced by deploying this source. No production migration is
performed by this application.

Requires Python 3.12 and dependencies in `requirements.lock`.

```sh
python -m hysteria_runtime auth --config /etc/vpn-hysteria/runtime.json
python -m hysteria_runtime collector --config /etc/vpn-hysteria/runtime.json
python -m hysteria_runtime collector --config /etc/vpn-hysteria/runtime.json --once
python -m unittest discover -s tests -v
python build_artifact.py --output-dir /tmp/hysteria-runtime-artifact
```

The JSON configuration must be an owned regular file with mode `0400` or
`0600`. Symlinks are rejected. It contains `mongo_uri`, `database`, `collection`,
`listen_host`, `listen_port`, `stats_url`, `stats_secret`, `epoch_file`,
`interval_seconds` and `lock_file`. Defaults use the existing loopback ports
and `blitz_panel.users` schema. Runtime files and configuration containing
credentials must remain outside Git. Only the statistics HTTP endpoint and
auth listener may use loopback addresses; they are not public APIs.

`POST /auth` consumes Hysteria's JSON callback and checks MongoDB every time.
It preserves valid legacy account semantics: blocked users are denied,
password comparison is constant-time, `unlimited_user` bypasses quotas and
dates, zero quota and zero days mean unlimited, and expiry uses UTC day
precision. Missing first-use dates do not prevent authentication. Invalid
numeric state and malformed nonempty dates fail closed. Auth requests and
readiness checks never activate users or modify counters. `GET /healthz`
checks process liveness; `GET /readyz` checks MongoDB. Bodies are bounded to
16 KiB, chunked requests are rejected and errors do not contain credentials.

An optional MongoDB `max_sessions` policy is `null` or absent for unlimited
sessions, or an integer from 1 to 128. Valid BSON `Int64` values are supported;
booleans, zero and malformed policies deny authentication. A limited account's
auth response includes `max_sessions`, including for `unlimited_user` accounts.
This does not activate an account or write counters. `GET /capabilities` reports
`max_sessions: true`, `version: 1` and `policy_source: mongo_max_sessions` without
authenticating an account or querying MongoDB.

The runtime supplies policy; the patched Hysteria server enforces simultaneous
QUIC client sessions atomically by auth ID within one server instance (the
official application creates one instance per process). The
node-agent must verify both that server's authenticated statistics capability
and this auth callback's capability before provisioning a limited account.
An older server or auth callback is unsupported for session limits. Multiple
TCP streams within one client session share one slot; an IP address is not a
device identity. The limit is not a cross-process or cross-node quota.

Each limited HTTP authentication also verifies the current engine's protected
statistics `/capabilities` endpoint. Only an exact supported capability grants
access. A missing endpoint, downgrade, invalid response, or unavailable checker
denies the limited account, so a saved limit cannot silently become unlimited.
Unlimited accounts keep their legacy authentication behavior and do not depend
on this engine capability check.

While tightening an existing session limit, the bridge may persist
`_vpn_session_close_pending: true` until native connection closure succeeds.
Authentication fails closed while this marker is present, including for
`unlimited_user` accounts. The collector preserves it; the bridge clears it
after a successful close so a failed operation can safely be retried.

The collector calls only `/traffic?clear=false` and `/online`. Upload/download
deltas and a per-user epoch/counter cursor are committed in one MongoDB atomic
compare-and-set update. Replaying a snapshot after an uncertain write does
not add its bytes again. `$inc` preserves concurrent counter changes, while
policy fields such as password, blocked state, expiry and quota are untouched.
Only activity observed for an account **without** `account_creation_date`
initializes that date. A present `null` date is retained for compatibility.

Every Hysteria process start must atomically write a **new unique epoch** to
the shared `epoch_file` before opening its statistics listener. The collector
checks that this epoch remains stable across a snapshot. Only one collector
may own `lock_file`; replacing an existing destructive collector requires a
separate coordinated migration. Same-epoch counter regression is an error,
not permission to reset or silently double-count usage.

This is delayed operational accounting, **not exact byte-based billing**.
Unobserved traffic immediately before a server crash/restart can be lost.
A duplicate database write is prevented, but collecting statistics cannot
recover bytes discarded by the server. The default interval is 60 seconds.
Changing interval does not create a durable per-packet audit log. Polling,
MongoDB outages and process restarts can delay first-use and usage observation.

This runtime never invokes `/kick`. Quotas and suspension deny fresh
authentication; termination of already-open streams is not guaranteed. The
legacy Hysteria next-traffic kick flag is unsuitable for reversible suspension.

Schema compatibility reference:
[Blitz auth source](https://github.com/ReturnFI/Blitz/blob/5f86cc90c8eb17369585a1c0942a7539507a9d61/core/scripts/auth/user_auth.go).
This is an independent implementation; the archived/modified legacy binary
is not copied into the new image. Dependency versions were verified against
[official PyMongo PyPI metadata](https://pypi.org/project/pymongo/) and
[dnspython metadata](https://pypi.org/project/dnspython/).

`build_artifact.py` produces a deterministic tar.gz with `app/` prefix and an
external release manifest containing SHA256, source commit and app working-tree
dirty state. Only the runtime package, dependency lists and this README are
included; tests, Git metadata, private files and credentials are excluded.
Extract this archive into its own runtime release directory, rather than
overwriting the node-agent's independent dependency lockfile.

Native Hysteria integration tests can use the actual runtime HTTP handler
backed by a disposable local MongoDB database:

```sh
PYTHONPATH=apps/hysteria-runtime python apps/hysteria-runtime/tests/serve_native_fixture.py \
  --ready-file /tmp/hys-native-ready.json
```

Run this from the repository root with the runtime dependencies installed and
MongoDB listening on loopback. `HYS_NATIVE_MONGO_URI` defaults to
`mongodb://127.0.0.1:27017/`; remote hosts, SRV, replica discovery, proxy options
and an existing database path are rejected. The helper always selects a new
random `vpn_hys_native_...` database and binds HTTP to `127.0.0.1` on an ephemeral
port. Its private ready file contains `auth_url`, public test-user credentials
and the generated database name, never the MongoDB URI. Limited and independent
users have a genuine BSON `Int64(1)` session limit; the unlimited user has `null`.
SIGTERM or SIGINT closes the listener and drops only that generated database.

Set `HYS_NATIVE_ENGINE_READY` to the native harness's engine-ready JSON path.
The harness writes `stats_url` and `stats_secret` for the actual local Hysteria
statistics listener into that file before authenticating limited users. The
fixture rereads this file and checks the actual engine capability for each
limited authentication; a missing or invalid file denies access. The JSON
must contain exactly those two fields, with no MongoDB URI or credentials.
