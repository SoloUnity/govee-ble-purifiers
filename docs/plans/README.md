# Implementation Plans

These documents describe ordered work. A plan's status line identifies whether
it remains proposed or has been implemented in the working tree. It is not
released behavior until its completion criteria are tested, documented, and
included in a release.

Implement the phases in order:

1. [Fan UI model](01-fan-ui-model.md) — expose Sleep through Turbo as five fan
   levels and retain Manual/Auto presets.
2. [JSON model profiles](02-json-model-profiles.md) — move purifier-specific
   identity, protocol, security, capability, and timing data into validated
   bundled profiles.
3. [Custom Auto](03-custom-auto.md) — add an opt-in PM2.5 hysteresis controller
   and settings flow. **Implemented in the working tree; not a release claim.**
4. [Custom Auto memory](04-custom-auto-memory.md) — persist only whether Custom
   Auto is active, never its prior fan target or policy history. **Implemented
   in the working tree; not a release claim.**

Each phase must update user and architecture documentation as its behavior is
implemented. The protocol document changes only when new wire-level evidence is
established.

## Decision gates

- Phase 1: the Manual-preset fallback from Auto/unknown is approved as Low
  (40%).
- Phase 3: approved and implemented using distinct authoritative revisions and
  event-driven observations plus bounded one-shot `aa 19` requests under the
  maintainer-approved implementation assumption that the response supplies
  authoritative PM2.5. This is not new protocol evidence, and there is no fixed
  PM2.5 cadence.
- Phase 3: approved and implemented switch-off handoff requests hardware Auto
  only while the purifier is already powered on and usable; it never powers on
  the purifier for handoff.
- Phase 4: implemented with per-config-entry ON/OFF-only memory, verified Store
  readback, fresh activation/connection barriers, and registry-gated cleanup.
  It adds no wire-level fact and no periodic polling.
