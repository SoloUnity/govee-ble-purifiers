# Phase 3 — Opt-In Custom Auto

**Status: implemented in the working tree. This status does not claim a release
or version change.**

## Goal

Add one integration-managed **Custom Auto** switch per purifier. The feature is
absent by default and exists only when the user opts in during setup or later in
the integration's settings.

When active, Custom Auto maps authoritative PM2.5 observations to five existing
purifier modes:

| Level | Fan mode |
| --- | --- |
| 1 | Sleep |
| 2 | Low |
| 3 | Medium |
| 4 | High |
| 5 | Turbo |

Custom Auto is Home Assistant policy, not a new protocol mode. All resulting
changes use the existing coordinator and reliable-client command path.

## Dependencies

Phase 3 was implemented after:

- Phase 1 has frozen the five-level Manual/Auto fan UI.
- Phase 2 supplies a validated immutable profile, capabilities, and these
  profile-backed defaults:

| Model baseline | PM2.5 boundaries (µg/m³) | Upshift | Downshift delays |
| --- | --- | --- | --- |
| H7124/plaintext | 3, 5, 9, 15 | 3 seconds | 7, 5, 5, 5 minutes |
| H7129/encrypted | 7, 9, 13, 19 | 3 seconds | 7, 5, 5, 5 minutes |

The four downshift delays apply respectively to Low→Sleep, Medium→Low,
High→Medium, and Turbo→High. Mutable per-entry values live in options; defaults
must never be duplicated in Python or localization strings.

## Approved implementation decision

The maintainer approved the sample count, PM2.5 source, and handoff in
[decision 0001](../decisions/0001-custom-auto-sampling-and-handoff.md). Tests
encode the approved behavior.

### Approved confirmation policy

- **Upshift:** after a level is confirmed, when the configured confirmation is
  positive, require two distinct authoritative PM2.5 revisions separated by the
  confirmation window. The confirming reading selects the target and may jump
  several levels. Activation with unknown ownership uses its first fresh valid
  reading to select an initial target. A zero delay also permits the first valid
  reading for a later upshift.
- **Downshift:** one qualifying reading starts the configured dwell, but a fresh
  still-qualifying reading at or after maturity is required before slowing down.
  Dirtier air resets incompatible downward dwell.
- Equal numeric values count as separate readings only if delivered as separate
  authoritative air-quality events.
- Cached state, command-side publication, invalid/sentinel values, and stale
  connection callbacks never count.

This asymmetric policy reacts quickly to worsening air while refusing to slow
the fan based on old data.

### Approved PM2.5 source

The alternatives considered were:

1. startup/refresh and unsolicited `ee 19` observations only;
2. opt-in fixed-cadence `aa 19` sampling while Custom Auto is active; and
3. event-driven observations plus bounded one-shot `aa 19` requests at
   activation, confirmation, or matured-downshift boundaries.

Option 3 is implemented: authoritative event-driven observations plus bounded
one-shot `aa 19` requests at activation, confirmation, and matured-downshift
boundaries. Treating the response as authoritative PM2.5 is an approved
implementation assumption supplied by the maintainer, not new wire evidence.
The implementation adds no fixed `aa 19` cadence and makes no protocol-document
claim from that assumption.

The one-shot path is bounded, serialized by the existing one-owner scheduler,
preemptible by user controls, and preserves the `aa 01` health poll.

## Configuration and entity exposure

### Setup

After purifier selection and validation:

1. Show **Enable Custom Auto**, default off.
2. If off, create the normal entry with no Custom Auto entity.
3. If on, show a separate settings step populated from the resolved profile:
   four boundaries, upshift confirmation, and four downshift delays.
4. Validate integer ranges and strictly ascending boundaries.
5. Store mutable settings in `ConfigEntry.options`; address/model remain in
   `ConfigEntry.data`.

Use separate steps rather than frontend-dependent conditional fields.

### Later settings

Add an options flow with the same enable switch, values, defaults, and
validation:

- Missing option means disabled for existing entries.
- Enabling reloads the entry and creates one switch.
- Disabling stops the controller, clears operational intent, removes the entity
  and its registry entry, and reloads atomically.
- Re-enabling starts inactive; Phase 4 may later restore state only across normal
  reload/restart, never across an explicit feature disable.
- Register one update listener and avoid listener accumulation.

The approved handoff when disabling an active feature requests hardware Auto if
the purifier is powered on and usable. If it is off, intent is cleared without
powering it on; unknown power or unavailability records that the handoff was not
attempted, and a command error records failure.

### Custom Auto switch

- Add the switch platform but create no switch when the feature is disabled.
- Use stable unique-ID suffix `custom_auto`, translated name **Custom Auto**, and
  the purifier's device association.
- Read cached controller state only; never perform Bluetooth I/O in properties.
- Follow coordinator availability while retaining internal intent during a
  temporary disconnect.
- ON activates the controller; OFF performs the approved handoff.
- Remain logically ON while suspended because the purifier is powered off.
- Never add Custom Auto to the protocol `FanMode` enum.

The fan entity continues to show the purifier's actual Sleep–Turbo percentage
and Manual preset while policy is active. The separate switch communicates
Custom Auto ownership; the fan must not falsely report hardware Auto.

## Architecture

Separate pure policy from lifecycle and I/O:

```text
PM2.5 observation
        ↓
pure hysteresis policy
        ↓
Custom Auto controller
        ↓
coordinator fan-mode request
        ↓
existing serialized reliable client
```

Suggested responsibilities:

- typed option parsing and profile defaults;
- pure PM2.5-to-level/hysteresis decisions with no tasks or I/O;
- controller activation, sample revisions, timers, target deduplication,
  configuration generations, command gating, and diagnostics.

The controller never accesses Bluetooth or the protocol codec. Coordinator
callbacks record observations and schedule evaluation; they must not await a
controller lock or issue nested BLE operations.

Expose bounded typed provenance so the controller can distinguish startup mode
restoration, exact acknowledgements, integration-caused notifications, genuine
physical changes, PM2.5 sample revisions, and connection generations.

## Hysteresis rules

- Above a boundary selects the next faster level; equality qualifies for the
  lower level after its downshift rule.
- One observation may request a multi-level upward jump.
- Downshift timers may mature independently, but the controller chooses only a
  target justified by the latest confirming sample.
- Dirtier readings cancel incompatible pending downshifts.
- Repeated identical targets are deduplicated.
- After an automatic command failure, wait for a new valid sample or completed
  availability recovery; never tight-loop.
- Invalid, missing, or PM2.5 values above 999 never drive a command.

## Physical and Home Assistant controls

### Physical Auto interception

When the feature is enabled **and the Custom Auto switch is ON**, a genuine
physical `ee 05` selection of hardware Auto must not leave the purifier there:

1. Keep Custom Auto active.
2. Obtain/evaluate a fresh authoritative PM2.5 sample.
3. Queue the corresponding Sleep–Turbo command through the coordinator.
4. If no fresh sample is available, remain armed and wait rather than using stale
   PM2.5 or a prior target.
5. Record pending, confirmed, or failed redirection in diagnostics.

The notification callback only schedules this work. Startup restoration of Auto
and notifications associated with an integration command must not be mistaken
for a physical button press.

If Custom Auto is OFF, physical Auto remains hardware Auto.

### Ownership and overrides

- Physical Sleep/Low/Medium/High/Turbo is a manual override and turns Custom
  Auto off after an authoritative observation.
- A Home Assistant percentage request turns Custom Auto off before applying the
  requested level.
- Home Assistant's explicit Auto preset turns Custom Auto off and requests
  hardware Auto. Physical Auto remains the special redirect above.
- Power off suspends an active controller and cancels timers without issuing a
  fan command.
- Power on resumes only after availability and a fresh PM2.5 observation.
- Failed handoffs must keep entity state truthful and report the pending/failure
  condition.

## Poor-signal and lifecycle behavior

- Do no work until essential initialization and application-channel readiness.
- On disconnect, make the switch unavailable but preserve active intent.
- Cancel/invalidate timers and samples tied to an old connection generation.
- Do not use PM2.5 cached from before a reconnect or option reload.
- Resume only from a fresh valid observation after recovery.
- Route all automatic commands through existing deadlines, send budgets,
  acknowledgement matching, reconciliation, and advertisement-aware recovery.
- User controls retain priority over refresh and policy work.
- Option reload/unload cancels and awaits all controller tasks/listeners before
  coordinator shutdown.
- Configuration generations prevent old timers from acting after settings
  change.

## Diagnostics

Add a bounded, secret-free `custom_auto` section with feature exposure, runtime
state, accepted sampling policy, underlying fan mode, last PM2.5 revision/value/
generation/age, current target, effective thresholds/delays, pending
confirmation/downshift, command state, physical override provenance, Auto
redirect status, and task/listener counts.

Continue redacting device identity and all H7129 session material.

## Tests

### Configuration and entity

- Disabled-by-default setup and existing entries expose no switch.
- Setup/options enable, validate, reload once, create exactly one stable entity,
  edit values, disable, remove the entity, and stop all tasks.
- Invalid order, bounds, types, and localization errors are stable.
- Fan state continues showing the actual percentage while the switch shows
  Custom Auto ownership.

### Pure policy and sampling

- Test all model thresholds, boundary equality, direct upward jumps, mature
  downward selection, cancellation, zero delay, and approved sample-count rules.
- Distinct revisions with equal values count correctly; cached/invalid/stale
  observations do not.
- Test every old delay value and profile override.
- If any `aa 19` query is approved, test scheduler priority, bounded timeout,
  preemption, recovery, and absence of an unapproved fixed cadence.

### Runtime and controls

- Deduplicate targets and gate retries.
- Physical Auto redirects only while Custom Auto is ON and only from fresh PM2.5.
- Startup/acknowledgement notifications are not misclassified as physical.
- Physical manual and Home Assistant commands perform the defined handoff.
- Power suspension/resume, option reload, disconnect/reconnect, generation
  rejection, command races, and unload leave truthful state and no orphan tasks.
- Existing fan, light, sensor, protocol, reconciliation, and `aa 01` health-poll
  behavior remains green.

## Documentation checkpoints

1. Configuration slice: README opt-in/setup/settings behavior and defaults.
2. Policy slice: approved sample rule, PM2.5 source, thresholds, timings, and
   boundary semantics.
3. Runtime slice: architecture policy for ownership, priority, freshness,
   physical Auto interception, recovery, and cleanup.
4. Entity slice: entity table, actual-level display, overrides, and feature
   enable/disable consequences.
5. Diagnostics slice: fields and weak-signal/stale-PM2.5 troubleshooting.
6. Protocol document only if new wire evidence is established.

## Completion criteria

- No Custom Auto entity or task exists unless opted in.
- Approved hysteresis/sample behavior is deterministic and profile-driven.
- Physical Auto redirects only under the exact requested condition.
- Automatic work retains current reliability and command safety.
- Weak signal produces suspended/recovering behavior, not stale decisions or
  command storms.
- Documentation is updated with every implemented slice and all tests pass.

These Phase 3 criteria are implemented in the working tree. Phase 4 remains
future work: this phase does not persist activation, the previous fan target,
samples, timers, or policy history across reloads or restarts.
