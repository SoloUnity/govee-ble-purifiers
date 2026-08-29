# Bundled model profiles

Purifier-specific identity, Bluetooth, application-channel, protocol,
capability, timing, and Custom Auto defaults are defined in validated
JSON under `custom_components/govee_ble_air_purifier/model_profiles`. The files
are integration assets, not user configuration. They are never loaded from a
config entry, URL, or user-provided path.

## Current lineages

| Exact profile | Baseline | Selection prefix | Channel |
| --- | --- | --- | --- |
| `h7124` | `default` | `GVH7124` | Plaintext |
| `h7129` | `default-encrypted` | `ihoment_H7129_` | Fresh H7129 session per connection |

The two baselines are complete so exact profiles can contain small reviewed
overrides. They have no model or advertised-name prefixes and can never be
selected for discovery or runtime use. A baseline is a reuse mechanism, not
support for an unknown purifier.

## Ownership boundary

JSON owns model-specific values:

- display identity and explicit case-insensitive advertised-name prefixes;
- GATT service, notification, and write UUIDs;
- plaintext/encrypted channel selection and non-secret negotiation policy;
- registered codec, checksum, startup-mode, command, and response-matcher IDs;
- checksum-valid request frames and initialization/refresh order;
- the essential request and sole periodic request;
- capabilities and bounded runtime timing/retry values; and
- active Custom Auto policy defaults: the ordered Sleep–Turbo modes, four PM2.5
  boundaries, upshift confirmation, and four downshift delays.

Python owns the closed implementations selected by those identifiers, Home
Assistant scanner and route handling, Bleak clients, connection generations,
cleanup/cancellation, encryption and ephemeral keys, typed decoding, matching,
state authority, command scheduling, availability, and hard safety ceilings.
Profile data cannot contain callbacks, imports, executable templates, secrets,
session material, captures, or filesystem paths.

## Loading, inheritance, and selection

Setup loads `schema.json` and all four profiles as one unit off the Home
Assistant event loop. It rejects duplicate JSON keys, validates the source
envelopes, resolves inheritance, validates every complete effective profile,
then publishes one immutable process-cached registry behind a lock. Runtime
layers receive the same resolved `DeviceProfile` instance; they do not retain
raw dictionaries or reload files per connection.

Only these parent relationships are accepted:

- `h7124` extends `default`;
- `h7129` extends `default-encrypted`.

Objects merge recursively. Scalars replace parent values, and arrays replace
the entire parent array. Multiple inheritance, derived-to-derived inheritance,
cycles, missing/external/path-like parents, and `null` deletion are rejected.
File name, `profile_id`, schema version, and parent must agree.

Discovery matches only exact profiles' declared name prefixes. Matching is
case-insensitive, and overlapping prefixes are rejected when the registry
loads. Nameless advertisements and near misses do not select a model. Existing
entries resolve their stored `H7124` or `H7129` value to the corresponding exact
profile without migrating or rewriting entry data.

There is deliberately no fallback after an artifact or exact-selection error.
Config flow aborts with a reinstall/update message, and entry setup raises a
permanent setup error before stale-connection cleanup or any other Bluetooth
work. This prevents an encrypted model from accidentally using plaintext rules
or the reverse.

## Validation and safety ceilings

The bundled draft 2020-12 schema closes every object
(`additionalProperties: false`) and is loaded atomically with the profiles. The
strict decoder rejects unknown fields and constrains the JSON shape.
Semantic validation additionally checks inheritance, UUIDs, lowercase hex,
20-byte checksums, matcher requirements, request references and ordering,
capability types, prefix ambiguity, security/negotiation consistency, Custom
Auto bounds, and reviewed retry/timing ceilings.

Python retains hard maximums even if the JSON schema is edited: three attempts,
45 seconds for a connection attempt, 15 seconds for a request or protected
phase, 120 seconds for a command, 300 seconds for setup, 60 seconds for normal
backoff, and 300 seconds for the recovery-storm window. The effective values in
the bundled profile may be lower. The periodic descriptor must also be the
essential checksum-valid `aa 01` request.

H7124 remains plaintext. H7129 must have a negotiation policy and creates a new
ephemeral encrypted session for every connection. Keys and negotiation payloads
are generated and held only by Python and never appear in profile data or
diagnostics.

## Diagnostics

Diagnostics expose the requested model, exact profile ID, lineage, schema
version, support status, channel strategy, source basename, deterministic
SHA-256 fingerprint, capabilities, request names/counts, effective timings, and
GATT UUIDs. They do not expose a Bluetooth address, unredacted device name, raw
profile JSON, keys, randomness, protected negotiation frames, or captured
device metadata.

The fingerprint is computed from canonical resolved profile data. It lets a
support report identify the exact effective bundle without including that raw
data.

## Adding or changing a model safely

1. Establish device-specific values from reproducible captures or confirmed
   runtime evidence. Do not copy an undocumented meaning from another model.
2. Add only a reviewed exact profile and an explicitly approved baseline
   relationship. A new profile must not become discoverable through a generic
   baseline alias.
3. Use only registered channel, codec, matcher, command, and startup-mode
   strategies. Add a typed, bounded Python implementation first if a genuinely
   new strategy is required.
4. Update the schema and semantic validator together. Preserve secret-field,
   path, checksum, timing-ceiling, prefix-ambiguity, and fail-closed checks.
5. Add equivalence or evidence-backed vector tests for requests, matchers,
   commands, security selection, retries, capabilities, and timing behavior.
6. Run the standalone validator and the full repository checks. Restart or
   reload Home Assistant after installing a changed bundled profile; hot reload
   is intentionally unsupported.

Validate the shipped bundle with:

```bash
env PYTHONPATH=. .venv/bin/python scripts/validate_model_profiles.py
```

The profile values are the active defaults when a user opts in to Custom Auto.
H7124 inherits boundaries `3, 5, 9, 15`, a three-second upshift confirmation,
and downshift delays `7, 5, 5, 5` minutes from `default`. H7129 inherits
boundaries `7, 9, 13, 19` with the same confirmation and delays from
`default-encrypted`. Enabling the feature writes the complete mutable setting
set to `ConfigEntry.options`; edits remain there and never modify bundled JSON.
A missing enable option means disabled. Profile defaults alone do not create an
entity, timer, query, or fan command.

The existing ownership, inheritance, exact-selection, and fail-closed rules are
unchanged. In particular, mutable per-entry values live only in options; entry
data continues to contain identity, and neither a baseline nor a malformed
artifact can be used as a runtime fallback.
