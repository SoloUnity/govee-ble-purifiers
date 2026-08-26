# AGENTS.md

## Scope

These instructions apply to the entire repository.

## Read before making changes

Use the existing documentation as the source of truth:

- [README.md](README.md) — user-facing behavior, setup, recovery, and
  development commands.
- [Protocol documentation](docs/govee-ble-air-purifier-protocol.md) —
  trace-supported Govee protocol facts.
- [Bluetooth architecture and policy](docs/home-assistant-bluetooth-expectations-and-api.md#19-policy-used-by-this-purifier-integration)
  — implementation architecture, timing, retries, lifecycle, and poor-signal
  handling.
- [Home Assistant integration reference](docs/home-assistant-bluetooth-integration-reference.md)
  — general Home Assistant and HACS requirements.
- [Implementation plans](docs/plans/README.md) — ordered proposals for future
  work; planned behavior is not current behavior until implemented and released.

Inspect the implementation and tests as well as the documentation. When they
disagree, identify the discrepancy rather than silently choosing one.

Do not treat a plan as implemented behavior. Use the current README,
architecture policy, implementation, and tests for the released contract.

## Project rules

- Preserve the separation between Bluetooth transport, plaintext/encrypted
  sessions, protocol decoding, the reliable client, coordinator, and Home
  Assistant entities.
- H7124 uses the plaintext protocol. H7129 uses a newly negotiated encrypted
  session for every connection.
- Home Assistant owns Bluetooth scanning and route selection. Do not create a
  private scanner or permanently bind a device to one adapter or proxy.
- Treat disconnects, weak signals, unplugged devices, adapter resets, and
  temporary ownership by the Govee app as recoverable conditions.
- Do not make entities available until the application channel is usable and
  essential initialization has completed.
- Only `aa 01` may be periodically polled. Do not introduce additional
  steady-state polling without new protocol evidence and explicit approval.
- Preserve command serialization, bounded sends, state reconciliation,
  advertisement-aware recovery, and defensive address-level cleanup.
- Never log H7129 keys, negotiation secrets, or decrypted sensitive material.

Detailed timing and retry policies belong in the linked Bluetooth architecture
document.

## Protocol evidence

The protocol document is evidence-only.

- Add protocol claims only when supported by supplied traces, reproducible
  captures, or confirmed runtime observations.
- Clearly distinguish observed facts from hypotheses.
- Do not infer undocumented meanings from similar frames or from the other
  purifier model.
- Unknown values must remain unknown rather than being guessed.
- Keep startup responses, command acknowledgements, and unsolicited physical
  notifications distinct.
- If behavior changes without establishing a new wire-level fact, update the
  architecture or README, not the protocol document.

## Home Assistant behavior

- Use Home Assistant's shared Bluetooth APIs and current best connectable route.
- Keep config-flow validation temporary and independent from the configured
  entry's runtime connection.
- Configured entries must load without waiting indefinitely for the purifier and
  recover in the background.
- Expected recovery failures should not flood Home Assistant's error log.
- User-visible failures must include useful, secret-free diagnostic context.
- Cleanup must cover failed connection attempts, unload, removal, shutdown, and
  the next startup after an unclean exit.

## Making changes

- Preserve unrelated user changes.
- Keep changes focused on the requested behavior.
- Add or update regression tests for every protocol, recovery, setup, lifecycle,
  or command-handling change.
- Prefer deterministic event-driven recovery over simply increasing every
  timeout.
- Keep stale callbacks, responses, and partial state scoped to their connection
  generation.
- Do not weaken availability or acknowledgement rules merely to hide errors.

## Documentation responsibilities

When implementation behavior changes:

- Update [README.md](README.md) for user-visible behavior.
- Update the implementation policy in
  [docs/home-assistant-bluetooth-expectations-and-api.md](docs/home-assistant-bluetooth-expectations-and-api.md#19-policy-used-by-this-purifier-integration).
- Update
  [docs/govee-ble-air-purifier-protocol.md](docs/govee-ble-air-purifier-protocol.md)
  only when the change establishes new protocol evidence.
- Update the generic Home Assistant reference only when the referenced Home
  Assistant or HACS behavior has changed.

## Verification

Run:

```bash
env PYTHONPATH=. .venv/bin/ruff check .
env PYTHONPATH=. .venv/bin/pytest -q
python3 -m compileall custom_components tests
git diff --check
```

Also inspect the final diff for accidental changes and confirm that no secrets
or captured session material were added.

Do not run HACS or Hassfest container validation unless explicitly requested.

## Releases and Git

- Do not change the integration version unless a release is requested.
- Do not commit, push, tag, publish, or create a GitHub release unless explicitly
  requested.
- For a requested release, keep the manifest version and README release version
  synchronized.
- Never rewrite repository history or discard existing work.
