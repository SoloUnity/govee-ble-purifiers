# Implementation Plans

These documents describe proposed future work. They are not descriptions of
current released behavior until their completion criteria have been implemented,
tested, documented, and released.

Implement the phases in order:

1. [Fan UI model](01-fan-ui-model.md) — expose Sleep through Turbo as five fan
   levels and retain Manual/Auto presets.
2. [JSON model profiles](02-json-model-profiles.md) — move purifier-specific
   identity, protocol, security, capability, and timing data into validated
   bundled profiles.
3. [Custom Auto](03-custom-auto.md) — add an opt-in PM2.5 hysteresis controller
   and settings flow.
4. [Custom Auto memory](04-custom-auto-memory.md) — persist only whether Custom
   Auto is active, never its prior fan target or policy history.

Each phase must update user and architecture documentation as its behavior is
implemented. The protocol document changes only when new wire-level evidence is
established.

## Decision gates

- Phase 1: the Manual-preset fallback from Auto/unknown is approved as Low
  (40%).
- Phase 3: approve the PM2.5 sampling and confirmation strategy before controller
  implementation. The current recommendation is documented in that plan.
- Phase 3: confirm the handoff behavior when Custom Auto is turned off; hardware
  Auto while powered on is recommended.
