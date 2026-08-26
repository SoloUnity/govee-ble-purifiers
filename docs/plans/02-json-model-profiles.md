# Phase 2 — Data-Driven Model Profiles

## Goal

Move purifier-specific identity, Bluetooth, application-channel, protocol,
capability, timing, and future Custom Auto defaults out of scattered Python
conditionals into bundled, validated JSON profiles.

This phase changes where established behavior is defined. It must not broaden
discovery, infer new protocol facts, or change current H7124/H7129 wire behavior.

Required bundled files:

- `default.json`: complete plaintext baseline based on H7124.
- `h7124.json`: verified H7124 identity layered over `default`.
- `default-encrypted.json`: complete encrypted baseline based on H7129.
- `h7129.json`: verified H7129 identity layered over `default-encrypted`.

Defaults reduce duplication; they do not make unknown purifiers supported.

## Non-negotiable compatibility

- H7124 remains plaintext and H7129 negotiates a fresh encrypted session for
  every connection.
- Existing request frames, ordering, matchers, mode decoding, acknowledgements,
  retries, availability, recovery, and command reconciliation remain equivalent.
- Only `aa 01` remains periodically polled.
- Existing config entries, unique IDs, entity IDs, titles, and stored H7124/H7129
  model values continue without migration.
- No profile contains H7129 keys, session material, random negotiation payloads,
  decrypted captures, arbitrary code, imports, or user-provided paths.
- An invalid exact profile fails closed before Bluetooth work; it never silently
  falls back to a plaintext or encrypted default.

## Ownership boundary

JSON owns device-specific data and selects closed Python strategies:

- advertised-name families and display identity;
- GATT service/notify/write UUIDs;
- plaintext versus encrypted channel selection;
- non-secret negotiation policy and timing;
- frame length/checksum strategy identifiers;
- Auto parameter and startup fan-mode assembly strategy;
- named request catalog, initialization/refresh order, essential request, and
  sole periodic request;
- command frames/templates and closed response matcher definitions;
- capabilities and supported fan modes;
- model/application timing and bounded retry values; and
- Custom Auto default thresholds, levels, and delay values for Phase 3.

Python continues to own Home Assistant scanner/route mechanics, Bleak clients,
connection generations, cleanup, cancellation, encryption algorithms and keys,
typed decoding, matcher implementations, state authority, command scheduling,
availability, and hard safety ceilings.

Setup scan duration and setup-form observation intervals remain integration-wide
Home Assistant UI policy rather than purifier-profile data.

## Files and types

Add:

```text
custom_components/govee_ble_air_purifier/
├── profiles.py
└── model_profiles/
    ├── schema.json
    ├── default.json
    ├── default-encrypted.json
    ├── h7124.json
    └── h7129.json
scripts/validate_model_profiles.py
tests/test_profiles.py
docs/model-profiles.md
```

Use strict immutable typed values at runtime. Runtime layers receive the same
resolved profile instance and never read raw dictionaries or reload files per
connection.

## JSON contract

Use JSON Schema draft 2020-12 with `additionalProperties: false` at every level,
plus semantic validation that JSON Schema cannot express.

Every file declares `schema_version` and `profile_id`. Root profiles contain a
complete profile. Exact profiles declare one allowed root through `extends` and
contain only overrides.

### Inheritance

- Only `default` and `default-encrypted` are roots.
- `h7124` extends only `default`; `h7129` extends only `default-encrypted`.
- Derived-to-derived, multiple, cyclic, missing, path-like, or external parents
  are rejected.
- Objects merge recursively; scalars replace; arrays replace as a whole.
- `null` is not a deletion mechanism unless a field explicitly permits it.
- Reject duplicate JSON keys.
- Validate the source envelope, resolve inheritance, then validate the complete
  effective profile.
- File name, profile ID, and parent must agree.
- Record lineage and a deterministic fingerprint for diagnostics.

### Effective sections

1. `identity`: manufacturer, model, display name, support status, and explicit
   case-insensitive advertised-name prefixes. Address prefixes never select a
   profile. Default roots initially have no discoverable aliases.
2. `bluetooth`: service/notify/write UUIDs and device-level connection/GATT
   bounds.
3. `channel`: `plaintext` or `h7129_session`, first-application delay, and the
   encrypted phase policy where applicable. Plaintext rejects negotiation data.
4. `protocol`: closed codec/checksum identifiers, frame size, Auto parameter,
   startup mode strategy, request catalog, ordered sequences, essential and
   periodic request names, command definitions, and closed matcher kinds.
5. `capabilities`: explicit power, fan, light, PM2.5, filter-life, unsolicited
   update, and refresh support. Do not infer capability from a frame's presence.
6. `timings`: request, initialization, poll, command, advertisement admission,
   recovery, GATT, cleanup, and encrypted negotiation timing values.
7. `custom_auto_defaults`: the five ordered modes, four PM2.5 boundaries,
   upshift confirmation, and four downshift delays used by Phase 3.

JSON may select only registered strategy/matcher identifiers. It must not carry
regular-expression frame programs, callbacks, import paths, Python names, or
unrestricted templates.

## Bundled Custom Auto defaults

Encode these now so Phase 3 never duplicates them in Python:

| Root | PM2.5 boundaries (µg/m³) | Upshift | Downshift delays |
| --- | --- | --- | --- |
| `default` | 3, 5, 9, 15 | 3 seconds | 7, 5, 5, 5 minutes |
| `default-encrypted` | 7, 9, 13, 19 | 3 seconds | 7, 5, 5, 5 minutes |

The ordered target modes are Sleep, Low, Medium, High, Turbo. Exact H7124 and
H7129 profiles inherit these values unless evidence-backed model overrides are
later introduced.

Validate strictly ascending boundaries, PM2.5 values from 0 through 999,
upshift confirmation from 0 through 300 seconds, and downshift delays from 0
through 1440 minutes.

These are inactive defaults in Phase 2; they create no entity, controller,
timer, query, or command until Phase 3 is implemented and opted in.

## Loader and selection

- Load all four profiles and the schema atomically before Bluetooth work.
- Decode and validate off the event loop, then cache one immutable registry per
  Home Assistant process behind a lock.
- Match names only against exact profiles' declared prefixes.
- Preserve current fresh/nameless advertisement identity handling.
- Resolve configured H7124/H7129 entries to their exact profiles without
  rewriting entry data.
- Never load user, remote, or config-entry-provided JSON.
- Do not hot-reload. Integration reload/restart is required for bundled changes.
- Profile artifact failures are permanent setup errors with translated,
  actionable messages, not transient Bluetooth failures or recovery loops.

## Staged migration

1. Write `docs/model-profiles.md`, the schema contract, and AGENTS guidance.
2. Implement strict decoding, inheritance, semantic validation, immutable types,
   registry selection, and canonical fingerprints.
3. Add all four JSON files, Custom Auto defaults, validator, and equivalence
   tests while Python constants remain authoritative.
4. Resolve profiles in config flow and entry setup before Bluetooth operations.
5. Pass one resolved profile through coordinator, GATT transport, channel,
   protocol, and reliable client.
6. Move UUIDs, security selection, requests, frames, capabilities, and timings
   layer by layer, keeping equivalence tests green after every slice.
7. Add safe profile diagnostics and translated artifact errors.
8. Remove superseded Python conditionals/constants only when no caller remains;
   never leave two authoritative sources at phase completion.

## Validation and safety

Reject unknown fields, duplicate keys, missing required data, invalid UUIDs or
hex, incorrect frame length/checksum, unknown matcher/template variables,
unsafe timing/attempt values, ambiguous name prefixes, unresolved request names,
unsupported schema versions, and secret-like fields.

Keep hard Python assertions for critical invariants even after profile
validation:

- exactly one periodic request;
- periodic request is the essential `aa 01` descriptor;
- bounded attempts and timeouts do not exceed reviewed ceilings;
- encrypted profiles require negotiation and plaintext profiles forbid it;
- exact profiles cannot fall back after an error.

## Diagnostics

Expose only safe resolved metadata: requested model, profile ID, lineage, schema
version, support status, security strategy name, source basename, fingerprint,
capabilities, request names/counts, effective timings, and protocol UUIDs.

Never expose addresses, unredacted device names, raw arbitrary JSON, keys,
negotiation randomness, encrypted/decrypted negotiation frames, or captured
device-specific metadata.

## Tests

- Validate all files, inheritance, replacement semantics, strict fields, duplicate
  keys, paths, schema versions, UUIDs, frames, checksums, templates, timing
  ceilings, capabilities, references, and secret rejection.
- Test exact supported and near-miss names, nameless traffic, ambiguity, default
  non-discoverability, and fail-closed exact profiles.
- Prove H7124/H7129 command vectors, initialization/refresh ordering, matcher
  behavior, Auto parameters, startup mode assembly, and unknown-value behavior
  are byte-for-byte equivalent.
- Prove GATT/channel selection, negotiation, retries, poll timing, recovery,
  availability, and command behavior consume the resolved profile without
  regression.
- Prove only `aa 01` is periodic.
- Test old version-1 entries without migration or mutation.
- Test atomic/cached loading, no Bluetooth work on profile failure, same-instance
  propagation, packaging, safe diagnostics, and the standalone validator.

## Documentation and AGENTS checkpoints

1. Before code migration, add `docs/model-profiles.md` describing ownership,
   inheritance, fallback, validation, selection, security, timing ceilings, and
   safe model extension.
2. When JSON lands, document each lineage and cross-check every value against
   current implementation and protocol evidence.
3. As runtime layers migrate, update the Bluetooth architecture policy with
   loading lifecycle, effective timing ownership, failure classes, and
   diagnostics.
4. At completion, update README behavior/troubleshooting and reconcile all
   documented timing values. Protocol docs change only for new evidence.
5. Update `AGENTS.md` to require the profile policy/schema as sources of truth,
   fail-closed exact selection, evidence for profile values, no secret or
   executable profile content, profile validation/equivalence tests, and the
   standalone validator in required verification.

## Completion criteria

- Four profiles and the schema are bundled and validated.
- H7124 resolves through `h7124 -> default`; H7129 through
  `h7129 -> default-encrypted`.
- Unknown names remain unsupported and exact failures never fall back.
- Runtime layers share one immutable resolved profile.
- Existing entries and behavior are regression-equivalent.
- Custom Auto defaults exist only as inactive data for Phase 3.
- No migrated device-specific value remains independently authoritative in
  Python.
- Profile diagnostics are useful and secret-free.
- Documentation, AGENTS guidance, validator, and full repository checks pass.
