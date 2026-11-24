# Permanent OpenVPN retirement

The owner shut down KZ and retired OpenVPN on 2026-11-24. The supported
product is now the NL node: BASIC provides Hysteria with one simultaneous
connection; PLUS provides Hysteria and VLESS REALITY.

`scripts/retire_openvpn.py` is an explicit operator migration, not a startup
hook or a force-revoke API. It does not need the old Manager to be reachable.
It makes no network calls, financial entries, profile exports or Telegram sends.

## Execution contract

1. Take a database backup. Run `OpenVPNRetirement(uow).preview()` using the
   installed application's database settings and save its JSON privately.
2. Review the exact manifest. Every retained OpenVPN credential must already
   be suspended, its account must have an existing suspended, expired or
   cancelled subscription, and there must be no running legacy operation.
   Modern credentials, access pointers and pending provisioning dependencies
   must not reference credentials being archived. Referenced terminal node
   operations are checked too. Unverified orphan dependencies require separate
   review; a shutdown is never evidence of successful certificate revocation.
3. Call `apply(manifest, batch_id=..., actor_user_id=..., reason=...)` with an
   active owner actor and the owner's permanent-shutdown attestation. PostgreSQL
   takes short table locks, checks the entire manifest again, and commits the
   archive, detach and retirement together. A busy database or changed manifest
   aborts rather than applying a partial migration.
4. Verify all legacy operational configs are gone, old servers are `retired`
   with new issuance disabled, and all account plans/statuses and financial
   data are preserved. Keep the manifest and execution receipt privately.

The audit contains each former configuration's metadata and operation links,
explicitly marked `remote_revocation_verified=false`. Existing terminal
operation statuses and payloads remain historical evidence; pending/failed
legacy intents become `superseded`. The old server row and status history stay
available. The historical migration snapshot is retained and extended with
decommission evidence; validated legacy cutovers become complete.

No subscription is activated by this migration. A customer can subsequently
resume through the normal subscription service and receive the services in
their existing plan on NL. Only this later explicit action starts active access
and normal hourly billing. Global billing switches and billing guards do not
change.

Replaying the same batch and reviewed manifest is a no-op, including after a
customer resumes. It cannot restore previous paused states. A different
manifest with the same batch ID is rejected.

## Operational scope

The production preview on 2026-11-24 contained 34 suspended OpenVPN configs
belonging to 21 suspended subscriptions, all on legacy server 2. There were no
live node dependencies, legacy access pointers or node bindings to those configs.
Server 7 (`nl-1`) and its current accesses are outside the mutation scope.

Application compatibility code and historical OpenVPN records can remain in
the repository. They are not live dependencies: retired servers receive no
scheduled polling or new provisioning. Do not re-enable an old server or its
Manager with the former credentials; decommission does not claim they were
remotely revoked.
