# Custom Auto sampling and handoff

Status: accepted and implemented in the working tree (not a release claim).

Custom Auto uses source option 3: authoritative event-driven PM2.5 observations
plus bounded one-shot `aa 19` requests at activation, confirmation, or matured
downshift boundaries. Boundary events may request a sample but never issue a fan
command; only a fresh authoritative observation can do that. There is no fixed
PM2.5 polling cadence, and the existing `aa 01` health poll remains unchanged.
Use of the `aa 19` response as authoritative PM2.5 is a maintainer-approved
implementation assumption, not newly established wire evidence.

After a level is confirmed, positive upshifts require two distinct authoritative
revisions separated by the configured confirmation window; zero confirmation
permits the first reading. Activation with unknown ownership uses the first
fresh valid reading to choose an initial target. Downshifts start from one sample
and require a fresh still-qualifying sample at or after maturity.

Turning Custom Auto off, or disabling the option, requests hardware Auto only
when the purifier is already powered on and the application channel is usable.
When it is off, the integration clears Custom Auto intent without powering the
purifier on. Unknown power or temporary unavailability leaves the truthful OFF
state with handoff not attempted; command failure is retained as a failed
handoff.

## Runtime selection memory

Feature exposure and runtime ownership are separate. The config-entry option
controls whether the Custom Auto switch exists; an integration-owned Home
Assistant `Store` remembers only that switch's runtime ON/OFF selection. Storage
version 1 uses key `govee_ble_air_purifier.custom_auto.<entry_id>` and payload
exactly `{"active": true}` or `{"active": false}`. A fresh-store readback must
verify every save, and removal must verify absence. Failure is reported rather
than claiming a successful user-requested controller transition.

No speed, PM2.5 value or decision, policy band, target, timer, confirmation,
command, or policy history is stored. Restored ON and every OFF→ON activation
start a new activation/sample barrier and wait for a valid authoritative PM2.5
observation from the current connection before calculating a target. Bluetooth
loss preserves remembered ON but cannot issue a command without current data.

Normal unload, reload, Home Assistant restart, and shutdown preserve the
boolean. Feature or config-entry disable and config-entry removal clear it. A
missing, hidden, disabled, or removed stable switch registry entry closes the
command gate and runs serialized, generation-scoped deactivate/remove/handoff
cleanup; unhide or re-enable remains OFF. Activation and connection generations,
the lifecycle lock, final gate checks, and bounded cleanup ownership reject stale
work. This memory policy adds no polling and does not change Home Assistant's
shared Bluetooth route ownership.
