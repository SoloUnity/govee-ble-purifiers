# Phase 1 — Fan UI Model

**Status:** Implemented in the working tree; release status remains governed by
the integration manifest and README release notes.

## Goal

Change only the Home Assistant fan presentation and entity-level command mapping.
Preserve the current protocol, Bluetooth, client, coordinator, config-entry, and
entity-registry contracts.

The target mirrors the old integration's user-visible organization without
reusing its implementation: Sleep through Turbo are five fan levels, while
Manual and Auto are the presets.

Custom Auto, new settings, model profiles, transport changes, protocol changes,
and release/version work are out of scope for this phase.

## Target UI contract

| Purifier mode | Home Assistant percentage | Preset |
| --- | ---: | --- |
| Sleep | 20% | `manual` |
| Low | 40% | `manual` |
| Medium | 60% | `manual` |
| High | 80% | `manual` |
| Turbo | 100% | `manual` |
| Auto | unset | `auto` |
| Unknown | unset | unset |

The fan advertises five speed levels and exactly two presets in stable order:
`manual`, `auto`.

| Home Assistant request | Coordinator request |
| --- | --- |
| 20% | Sleep |
| 40% | Low |
| 60% | Medium |
| 80% | High |
| 100% | Turbo |
| 0% | Power off |
| Auto preset | Hardware Auto |
| Manual preset at an existing level | Preserve/reapply that level |
| Manual preset from Auto/unknown | Low (40%), approved for implementation |

Arbitrary nonzero percentages continue to use Home Assistant's ordered-list
conversion and therefore snap to one of these five levels. Documentation should
encourage canonical percentages.

Manual is only a Home Assistant grouping. It must not become a protocol
`FanMode`, frame, coordinator operation, or inferred device state.

## Work slices

### 1. Freeze the entity contract

- Replace the three manual levels with the ordered five-level list: Sleep, Low,
  Medium, High, Turbo.
- Set the fan speed count to five.
- Replace Auto/Sleep/Turbo presets with Manual/Auto.
- Report Manual for all five level modes and Auto only for hardware Auto.
- Keep 0% as power off and preserve power-on-before-mode behavior.
- Reject unknown presets before any coordinator call.
- Freeze the approved Manual-from-Auto fallback in tests and documentation.

### 2. Preserve protocol and runtime boundaries

- Reuse the existing six protocol modes and coordinator command path.
- Do not change polling, response matching, acknowledgement, connection recovery,
  or encryption.
- Keep the fan unique-ID suffix, entity ID, and device association unchanged.
- Do not introduce a config-entry migration.

### 3. Automation migration

Document the breaking UI/automation changes:

| Previous behavior | New behavior | Migration |
| --- | --- | --- |
| Sleep preset | 20% level | Use `fan.set_percentage: 20` |
| Turbo preset | 100% level | Use `fan.set_percentage: 100` |
| Low near 33% | Low at 40% | Use 40% |
| Medium near 67% | Medium at 60% | Use 60% |
| High at 100% | High at 80% | Change to 80%; 100% is Turbo |
| Auto preset | Auto preset | No change |
| No Manual preset | Manual preset | New UI grouping |

## Tests

- Table-test every physical mode and unknown state against percentage/preset.
- Assert five speeds and exactly `manual`, `auto` presets.
- Test exact 20/40/60/80/100 mappings and representative snapped values.
- Test 0%, power-on ordering, Auto, Manual preservation/fallback, unsupported
  presets, and `async_turn_on` argument precedence.
- Confirm startup and physical Sleep/Turbo updates appear as 20%/100%, not
  presets.
- Keep the existing protocol, client, coordinator, and recovery suites green.

## Documentation checkpoints

1. Update the README entity table, exact level mapping, and automation migration
   when the entity contract changes.
2. Update the integration policy document with the entity-layer mapping and state
   explicitly that Manual is not a wire mode.
3. Do not update the protocol document because this phase creates no protocol
   evidence.
4. Do not update the generic Home Assistant reference unless an upstream API
   requirement changes.

## Completion criteria

- The UI exposes five levels and Manual/Auto presets exactly as specified.
- Existing device/entity identities are retained.
- Automation migration is documented.
- No protocol, polling, Bluetooth, encryption, or recovery behavior changes.
- Required repository verification passes.
