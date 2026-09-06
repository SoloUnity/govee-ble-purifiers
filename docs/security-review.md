# Security review

Reviewed on 2026-09-06. Scope: all integration Python modules, profile loading,
Bluetooth transport/session ownership, frame parsing, command scheduling,
config flows, diagnostics, Custom Auto persistence, dependencies and CI. This
is a source review plus regression/static testing, not a security certification.

## Fixed in this contribution

- **Unbounded notification buffering:** the client now caps its queue at 256
  frames. Overflow invalidates the session, clears queued frames, marks it
  unavailable, and enters existing recovery. Further frames from that failed
  session are ignored; lost acknowledgements cannot become false success.
- **Diagnostic privacy:** exported nested backend errors redact MAC addresses,
  BlueZ device paths, known device names/IDs and raw 20-byte frame samples.
  Internal state is not mutated; counters remain useful.
- **Unbounded setup identity retention:** the cache keeps at most 256 recently
  observed supported identities across setup flows.
- **Profile consistency:** exact model identity must match the artifact name,
  and its channel must match the approved plaintext/encrypted lineage. This
  prevents malformed bundled profiles from silently changing channel type; it
  is not protection against an attacker who can replace integration code.
- **Contribution surface:** the optional development probe service was removed
  from this PR. Model support uses the existing setup and runtime paths.
- **CI/dependency integrity:** actions are pinned to commits, checkout does not
  persist credentials, jobs have timeouts, token permissions remain restricted,
  and test dependency downloads require the generated hashes.

Regression coverage includes flood/recovery behavior, old session rejection,
redaction without state mutation, cache eviction, malformed profile rejection,
and 2,048 generated bounded frame inputs across all four models. All original
upstream test modules remain included.

## Dependency audit: unresolved upstream constraint

`pip-audit` reported five records representing three distinct advisories for
`cryptography==48.0.1`, pinned by Home Assistant 2026.9.0 in the test fixture.
The current Home Assistant 2026.9.1 package also pins 48.0.1. The integration
adds no runtime package requirements and does not override that platform pin.

- [GHSA-m2h6-j472-rp4c](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c): X.509 DNS-name constraint validation.
- [GHSA-jwv3-5hgf-82ww](https://github.com/pyca/cryptography/security/advisories/GHSA-jwv3-5hgf-82ww): X.509 certificate path-building resource exhaustion.
- [GHSA-g6cj-pr64-35w5](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5): PKCS#7 EnvelopedData decryption oracle.

The audit feed recommends 49.0.0 for the first two and 50.0.0 for the third.
The first two upstream advisory pages list affected versions through 48.0.0,
which differs from the audit feed's flag on 48.0.1; that discrepancy is not
silently suppressed. Cryptography 50.0.1 is available, but installing it over
HA's exact pin would violate the tested dependency contract.

This integration calls AES cipher primitives, not X.509 verification or PKCS#7
APIs. No direct path from purifier frames to the affected APIs was found. That
limits applicability to this integration; it does not clear the platform or
make the dependency audit pass. Move to a compatible patched HA/fixture release
when available. Do not describe this environment as free of known advisories.

## Protocol and operational limits

Bandit reports the existing AES-ECB use as medium severity. It is the device's
wire protocol, combined with an RC4-compatible per-frame transform and XOR
checksum. Replacing it with authenticated encryption would require compatible
purifier firmware. H7124/H712C use plaintext. Model names, addresses and XOR
checksums are not cryptographic device authentication. Radio spoofing, replay,
interference and denial of service cannot be ruled out by this implementation.
Negotiation secrets remain connection-scoped and are not exported in diagnostics.

The other static findings are internal assertions, retry jitter using ordinary
randomness, and intentional exception handling. They were reviewed in context;
none supplies authentication, key generation or an authorization decision.
Session randomness uses `os.urandom`. No new shell execution, dynamic code
loading, network listener, cloud credential handling or remote profile loader
was introduced.

Debug logging remains opt-in and can contain device/application details; review
logs before sharing them. Redacted diagnostics do not sanitize separate log
files. H7123 intermittent disconnects remain an unresolved reliability issue,
documented in the model evidence record. No changes were deployed to a live
Home Assistant instance during this review.
