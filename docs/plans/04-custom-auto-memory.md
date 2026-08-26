# Phase 4 — Custom Auto Memory

## Goal

Persist one fact per config entry: whether the Custom Auto runtime switch was ON
or OFF.

Across an integration reload or Home Assistant restart:

- restore the selected ON/OFF state;
- never restore a fan speed, policy band, target, timer, confirmation, sample, or
  previous PM2.5 decision; and
- when restored ON, wait for a new authoritative PM2.5 observation from the new
  startup epoch and calculate the target from scratch.

The same clean activation applies whenever Custom Auto is turned OFF and later
ON. It must never jump back to the level used by the earlier activation.

## State separation

| State | Source | Persisted? |
| --- | --- | ---: |
| Feature enabled/entity exists | Config-entry option | Yes |
| Custom Auto runtime ON/OFF | Custom Auto memory | Yes |
| Hysteresis band | Controller runtime | No |
| Last/current fan target | Controller runtime | No |
| PM2.5 sample/decision | Controller runtime | No |
| Timer, confirmation, command | Controller runtime | No |

Explicitly disabling the feature removes the entity/controller and clears saved
runtime state. Re-enabling the feature starts OFF. Normal unload/reload preserves
the saved runtime state.

## Dependencies

Phase 4 begins only after Phase 3 exposes:

- stable feature-enable options and switch unique ID;
- explicit activate, deactivate, reset, and shutdown lifecycle boundaries;
- activation/configuration and connection generations;
- authoritative PM2.5 sample revisions;
- full reset of targets, timers, confirmations, and hysteresis history; and
- all fan commands through the existing coordinator/client path.

The persistence layer must not duplicate policy or command responsibilities.

## Persistence contract

Use an integration-owned Home Assistant `Store`, not config-entry options and not
`RestoreEntity`, for runtime ON/OFF state.

Recommended storage:

- key: `govee_ble_air_purifier.custom_auto.<entry_id>`;
- schema version: 1;
- payload: exactly `{"active": true}` or `{"active": false}`.

The entry ID avoids Bluetooth identity in storage and prevents a removed and
re-added entry from inheriting state accidentally.

A small memory owner loads, strictly validates, saves, and removes this boolean.
Missing, malformed, non-boolean, or incompatible data fails safe to OFF and
never triggers a fan command. Await writes so a completed switch service call is
durable before a restart. If a write fails, report failure and do not claim the
transition succeeded.

The payload must not grow policy fields without a separately reviewed schema
change.

## Startup ordering

For a feature-enabled entry:

1. Create the coordinator with empty authoritative device state.
2. Load Custom Auto memory.
3. Create the controller inactive and with no history.
4. Attach its observation listener before starting Bluetooth.
5. If stored ON, reset history, establish a new activation generation/sample
   barrier, and arm the controller without issuing a fan command.
6. Start background Bluetooth ownership and forward entity platforms.
7. Accept only a valid PM2.5 observation newer than the barrier and belonging to
   the new connection/startup generation.
8. Compute the target fresh and use the normal command path.

Stored OFF performs no evaluation. Missing PM2.5 leaves the switch armed/ON but
command-free until a valid observation arrives.

## Toggle behavior

### ON

Under one lifecycle lock:

1. Persist ON.
2. Clear every prior policy/runtime field.
3. Increment the activation generation and establish a fresh sample barrier.
4. Mark active without using coordinator-cached PM2.5.
5. Obtain/wait for a new authoritative PM2.5 sample through Phase 3's approved
   bounded mechanism.
6. Calculate and apply a fresh target.

### OFF

Under the same lock:

1. Persist OFF.
2. Invalidate the activation generation.
3. Cancel and await timers/evaluations.
4. Clear bands, samples, targets, and pending work.
5. Perform Phase 3's approved hardware handoff.

A later ON cannot reuse any prior field or callback.

## Recovery and unavailable data

- Bluetooth loss never changes the saved ON/OFF selection.
- The switch follows entity availability while its remembered selection remains
  internal.
- No command occurs while the application channel is unavailable.
- Reject samples, callbacks, timers, and command completions from old connection
  or activation generations.
- After reconnect, reconcile only from a valid new-generation PM2.5 observation.
- A reload/restart always discards policy history even if the saved boolean is
  ON.
- Invalid, sentinel, missing, or pre-activation PM2.5 keeps the controller armed
  without guessing or restoring a previous level.
- This phase adds no periodic poll; activation freshness uses Phase 3's approved
  bounded path.

## Cleanup policy

| Event | Saved ON/OFF state |
| --- | --- |
| Normal integration reload | Preserve |
| Home Assistant restart/shutdown | Preserve |
| Unexpected process exit | Preserve last completed write |
| Feature option disabled | Clear/remove |
| Feature re-enabled | Start OFF |
| Config entry removed | Clear/remove |
| Platform setup failure/retry | Preserve |

Before implementation, explicitly decide how Home Assistant config-entry disable
and entity-registry disable should behave. Recommended safety policy: a hidden or
explicitly disabled Custom Auto entity must not leave an active controller;
deactivate and clear its saved state, while ordinary reload remains preserving.

Normal unload must not remove storage. Entry removal must remove storage even if
runtime data is unavailable, then perform existing Bluetooth cleanup.

## Race safety

- Serialize toggle transitions and policy evaluation with one lifecycle lock.
- Every activation has a monotonically increasing token captured by samples,
  timers, evaluations, and automatic commands.
- Check token and active state immediately before sending.
- OFF invalidates the token before pending work continues.
- Rapid OFF→ON creates a new barrier that old observations cannot satisfy.
- Connection generation independently rejects stale Bluetooth work.
- Concurrent service calls serialize; the last successfully persisted change
  wins.
- Unload drains controller tasks/listeners before coordinator shutdown.

## Tests

### Storage

- Missing/malformed data defaults OFF; booleans restore exactly.
- Stored payload contains only `active`.
- Toggle completion awaits the write; write failure does not commit state.
- Feature/entry removal deletes the per-entry store.

### Restart and reload

- OFF restores inactive with no command.
- ON restores armed but sends nothing before a new authoritative sample.
- A new startup sample computes a fresh target.
- Prior fan level, band, PM2.5, timers, and targets cannot influence restoration.
- Normal reload preserves only the boolean and cannot miss immediate startup
  telemetry.

### OFF→ON reset

- OFF clears history and cancels work.
- ON rejects pre-activation samples and old callbacks/timers.
- A valid post-activation sample makes a fresh decision.

### Poor signal and cleanup

- Restored ON with an unavailable purifier remains armed without blocking setup.
- Reconnect preserves the boolean but requires new-generation PM2.5.
- Feature disable/re-enable, entry removal, concurrent toggles, unload, and a
  timer racing OFF leave no hidden controller or orphan work.

## Documentation checkpoints

1. README: explain ON/OFF-only memory, fresh recalculation, and armed/waiting
   state when Bluetooth or PM2.5 is unavailable.
2. Architecture policy: storage owner, startup ordering, activation/sample
   generations, reconnect, and cleanup.
3. Strings: distinguish enabling the feature/entity from turning the runtime
   controller on, and state that prior fan speed is never restored.
4. Developer architecture: record the exact versioned payload and lifecycle
   ownership.
5. Do not update the protocol document; this phase establishes no wire fact.

## Completion criteria

- Feature exposure and runtime ON/OFF are independent.
- Reload/restart restores only the boolean.
- No persisted artifact contains a fan level or controller history.
- Restored ON and OFF→ON both wait for post-activation authoritative PM2.5.
- Missing data never falls back to a previous target.
- Weak signal does not silently turn Custom Auto off.
- Stale work cannot issue commands.
- Explicit feature disable and removal cannot leave hidden active control.
- Repository verification and documentation updates pass.
