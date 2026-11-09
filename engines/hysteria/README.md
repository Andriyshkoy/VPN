# Hysteria session admission patch

Prepared candidate only. This directory does not deploy or restart anything.
The existing production service is unchanged until a separately approved rollout.

BASIC uses `max_sessions: 1` in the HTTP auth policy. The first authenticated
QUIC connection wins; a second connection with the same auth ID receives the
ordinary failed-auth response. TCP streams and UDP flows inside the first
connection share that one slot. PLUS uses `null` (unlimited). This is concurrent
connection admission, not physical device binding or an IP-address heuristic.

## Source and build

`source.json` pins official Hysteria `app/v2.13.0`, exact commit and source archive
SHA256, and the official Go Linux amd64 archive SHA256. `session-limit.patch` is
the entire patch, including native tests; its SHA256 is recorded beside it.
`UPSTREAM-LICENSE.md` retains the upstream MIT notice. Go modules retain upstream
versions/checksums and are verified with `go mod verify`.

On Linux amd64 with Python 3.12+, Git and a C compiler (for Go's race detector):

```sh
python3 engines/hysteria/build.py
```

The build installs Go into this directory's ignored cache, extracts a fresh
verified source archive, checks/applies the patch, runs changed Go packages with
`-race`, verifies modules, and writes a Linux amd64 binary, `SHA256SUMS` and
`build-manifest.json` to ignored `dist/`. It does not need system-wide Go or root.
`--cache-dir` and `--output-dir` support a separate local tools directory.
`--skip-tests` is only for rebuilding an already tested exact patch and is marked
in the manifest. No protocol format, client wire messages or cryptography changes.

## Lifecycle and compatibility

The optional `SessionLimitAuthenticator` leaves existing authenticators intact.
HTTP auth accepts an omitted/null/zero `max_sessions` as unlimited; positive
integers 1..128 set admission limits, malformed/out-of-range values deny auth.
The registry counts unlimited sessions as well as limited ones. Reservation and
limit checking share one mutex. Duplicate AUTH on one connection reuses its
reservation. QUIC connection completion releases its lease exactly once.
Disconnected peers release promptly after a graceful close; an unreachable peer
retains its slot until QUIC detects loss/idle expiry (default 30 seconds).

Scope is **auth ID per server instance**. The official app runs one instance.
Multiple independently running servers/listeners need shared coordination or
unique credential routing; this patch does not provide a distributed limit.
A limit change alone governs new admissions and does not evict existing sessions.
The node agent therefore blocks auth while tightening a policy, closes existing
sessions, then clears the pending marker. Retries complete a pending close.
For credential replacement, durably revoke the old credential before closing it.

## Protected management API

The existing traffic-stats handler, when attached to the patched core, adds:

- `GET /capabilities`: version 1, `max_sessions` reflects whether the actually
  installed authenticator supports session policy; `close_by_auth_id: true`,
  `session_scope: "auth_id_per_server_instance"`,
  `enforcement: "atomic_quic_connection"`.
- `POST /close`, body `["auth-id", ...]`: close all currently registered QUIC
  sessions for those IDs, response `{"closed": N}`. Auth callbacks begun before
  the close are fenced by a generation counter, so stale successful replies
  cannot bring a revoked session back. A future valid auth still needs the
  backend to permit it; `/close` itself is not persistent revocation.

Both routes require the configured traffic-stats secret via `Authorization`.
An empty secret or unattached controller returns 503. Keep this API on loopback.
Close generation tombstones are retained for the server's lifetime (one small
entry per distinct closed auth ID); a restart clears them with all connections.
`/kick` remains upstream-compatible and is not used for strict admission/close.

For every limited auth request, the runtime also checks the actual protected
engine capability, so downgrading the engine fails closed for existing keys.
Unlimited auth does not depend on this additional check.

The node agent checks both this engine capability and the actual HTTP auth
provider's policy capability before issuing a limited credential. Legacy engines
continue to serve unlimited profiles; requesting an unsupported limit fails.

## Verification

Native tests create real TLS/QUIC clients and a local TCP echo origin. They cover
24 competing clients with exactly one winner, 12 simultaneous TCP streams on
one client, independent IDs, unlimited/legacy auth, reconnect and repeated close,
duplicate AUTH, connection close during an auth callback, close-all, stale auth
reply after revoke, and a silent network loss followed by QUIC idle expiry.
Auth parser and protected management routes have HTTP tests.

The CI workflow additionally launches the actual Python runtime against a
random disposable MongoDB database, seeds BSON Int64 policies, and uses the
HTTP authenticator with native QUIC clients. It checks the full Mongo → runtime
→ HTTP authenticator → atomic admission path. The fixture only accepts loopback
Mongo URIs and drops its own random database on exit. Without the fixture,
`TestHTTPSessionLimitRuntimeMongoNative` reports a skip; core native tests still run.
CI only builds/tests/uploads a candidate artifact; there is no deployment job.
