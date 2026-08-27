# Govee BLE Air Purifier

A Home Assistant custom integration for private, local Bluetooth control of
Govee H7124 and H7129 air purifiers. No Govee cloud account or user-entered
Bluetooth address is required.

The integration follows the captured Govee protocol closely while adding
bounded connection recovery for weak signals, abrupt power loss, Home Assistant
restarts, and devices temporarily connected to the Govee app.

## Supported devices

| Model | Advertised-name family | Application channel |
| --- | --- | --- |
| Govee H7124 | `GVH7124*` | Plaintext 20-byte frames |
| Govee H7129 | `ihoment_H7129_*` | Fresh encrypted session for every BLE connection |

`*` means any sequence of characters. The advertised name determines the model;
the Bluetooth address is used only as the stable device identity. The integration
does not guess a model from an address prefix.

Other Govee models are not currently supported.

## Home Assistant entities

Each configured purifier creates four entities:

| Entity | Capabilities |
| --- | --- |
| Fan | Power, five percentage levels from Sleep through Turbo, Manual/Auto presets |
| Night light | Power, brightness, and RGB colour |
| PM2.5 sensor | Current particulate concentration in µg/m³ |
| Filter-life sensor | Remaining filter life as a percentage; diagnostic category |

Entities use the integration's cached push state and do not independently poll
Bluetooth. Physical controls update Home Assistant when the purifier sends the
corresponding notification.

The fan exposes the purifier's physical modes through this Home Assistant UI
mapping:

| Physical mode | Percentage | Preset |
| --- | ---: | --- |
| Sleep | 20% | Manual |
| Low | 40% | Manual |
| Medium | 60% | Manual |
| High | 80% | Manual |
| Turbo | 100% | Manual |
| Auto | unset | Auto |

Use the canonical percentages above in automations. Other nonzero percentages
snap to one of the five ordered levels. Selecting Manual preserves and reapplies
the current level; from Auto or an unknown mode it selects Low (40%). Manual is
only a Home Assistant grouping, not an additional purifier mode.

### Fan automation migration

The fan UI model changed from three percentage speeds plus Auto/Sleep/Turbo
presets to five percentage levels plus Manual/Auto presets. Existing automations
should be updated as follows:

| Previous request | Replacement |
| --- | --- |
| Sleep preset | `fan.set_percentage` with `percentage: 20` |
| Turbo preset | `fan.set_percentage` with `percentage: 100` |
| Low near 33% | `percentage: 40` |
| Medium near 67% | `percentage: 60` |
| High at 100% | `percentage: 80` (100% now selects Turbo) |
| Auto preset | No change |

## Requirements

- Home Assistant 2025.1.0 or newer
- A connectable local Bluetooth adapter or Bluetooth proxy that can see the
  purifier
- The purifier powered on and not connected to the Govee app

The purifiers accept only one Bluetooth central connection. Close the Govee app
before setup and normal Home Assistant use. Signal quality still matters even
when the purifier appears in Home Assistant's Bluetooth cache.

## Installation with HACS

1. In HACS, add this repository as a custom repository with category
   **Integration**.
2. Install **Govee BLE Air Purifier**.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services**.
5. Select **Add integration**, choose **Govee BLE Air Purifier**, and wait for
   the scan to finish. For another purifier, open the installed integration and
   select its blue **Add device** button.

For manual installation, copy
`custom_components/govee_ble_air_purifier` into the same path under the Home
Assistant configuration directory, restart Home Assistant, and follow steps 4
and 5 above.

This integration intentionally does not register Home Assistant manifest
Bluetooth discovery matchers. It therefore does not create the green, pending
**Discovered** cards that Home Assistant may retain after a device is no longer
currently visible. The explicit blue **Add device** flow is the only supported
setup path.

## How setup discovery works

Every new **Add device** flow performs a new scan; it does not reuse the choices
from a previous setup flow.

1. The integration registers a temporary listener on Home Assistant's shared
   Bluetooth scanner.
2. On supported Home Assistant versions, it requests a ten-second active scan.
   The integration owns the full ten-second observation deadline even if Home
   Assistant returns early because no `AUTO` scanner can open an active window.
   If the API is unavailable or the request fails, it still observes the shared
   scanner for all ten seconds.
3. Before the freshness window, it remembers any valid supported name already
   retained in Home Assistant's service-info cache or `BLEDevice`, together
   with supported names learned by earlier setup flows during the same Home
   Assistant session. These names establish identity only; they do not
   establish current reachability.
4. During the window it records fresh connectable addresses even when a packet
   is nameless or uses the Bluetooth address as its name. A valid supported name
   seen during the window is also retained and cannot be erased by a later
   nameless packet.
5. At the end of the window it also merges Home Assistant cache entries whose
   timestamps were refreshed during that same window. Older cached discoveries
   are excluded.
6. It combines current address-level sightings with known identities for the
   same normalized address. Already configured addresses and unresolved,
   unsupported, or non-connectable devices are removed. Remaining choices are
   sorted by RSSI: the strongest is labelled **Near**, and all others are
   labelled **Far**.
7. The choices are frozen while the user selects a purifier.
8. After selection, the integration waits up to ten seconds for a fresh,
   connectable advertisement from that exact address. It connects immediately
   when one arrives rather than waiting out the timeout. A fresh nameless packet
   is acceptable because the valid name and model were retained from the scan.
9. Setup validates the purifier by connecting and completing initialization
   before saving the config entry.

If no eligible purifier appears, start a new **Add device** flow after moving the
purifier or Bluetooth proxy closer. If a listed purifier disappears before it
is selected, the form reports that it is not currently visible instead of
starting from a stale route.

Home Assistant owns adapters, proxies, scanning, and route selection. This
integration does not start a private Bleak scanner and does not permanently bind
a purifier to the route that first saw it.

## Runtime communication

The integration maintains one serialized connection owner per purifier:

1. Wait for recent or newly observed connectable advertisement evidence.
2. Ask Home Assistant for the current best local-adapter or proxy route.
3. Clean up any stale address-level connection without touching a healthy
   client owned by the current runtime.
4. Create a fresh Bleak client and discover GATT services.
5. Subscribe to notifications before sending protocol traffic.
6. Use a plaintext H7124 channel or negotiate a fresh H7129 encrypted session.
7. Run the complete captured startup initialization sweep in order, allowing up
   to three attempts for every request.
8. Restore fan mode from the existing matched `aa 05` responses. H7124 resolves
   from `aa 05 00`; H7129 combines `aa 05 00` and `aa 05 01` only for manual
   Low/Medium/High modes.
9. Require the essential `aa 01` device-state response, but record an exhausted
   secondary request as best-effort and continue the rest of the sweep. A silent
   essential request receives at most three three-attempt batches on one
   connection before that session is recycled.
10. Mark entities available after the full sweep has been attempted and essential
   state is known. Values missed during secondary initialization remain unknown.
11. Process commands, unsolicited notifications, refresh requests, and the one
   documented periodic state query.

H7129 session keys exist only for one BLE connection. They are discarded on
every disconnect, negotiation failure, shutdown, or reconnect and are never
logged. Each `e7-01` and `e7-02` phase remains open for up to 15 seconds and
reuses the same protected request for as many as three sends about five seconds
apart. The first matching response completes the phase, including a delayed
response to an earlier send. Only phase exhaustion or an actual connection
failure recycles the GATT connection. A disconnect wakes the active negotiation
without producing a second orphaned-future error, and recovery diagnostics retain
the `e7-01` or `e7-02` phase that was interrupted.

### Polling and notifications

During normal operation, the only periodic request is the documented `aa 01`
device-state query on a fixed three-second write-to-write cadence. H7124
preserves the captured 1.936-second delay between startup and its first idle
poll; H7129 begins with the normal three-second delay. A missed periodic response
is retried up to three times on the existing connection. Exhausting all three
attempts treats the connection as unhealthy and enters normal reconnect recovery.

The integration does not continuously poll PM2.5, filter life, fan mode, or
night-light state. Those values come from startup initialization, a documented
H7129 refresh sweep triggered by `ee aa`, control confirmations, and unsolicited
device notifications.

## Poor-signal and disconnect handling

An unplugged purifier, radio loss, proxy loss, adapter reset, or connection by
the Govee app is treated as a transient disconnect:

- current entities become unavailable;
- the active protocol wait is interrupted immediately;
- stale notification and disconnect callbacks are rejected by connection
  generation;
- H7129 session material is invalidated;
- the active or partially connecting client is disconnected;
- stale local address-level connections are closed and verified when possible;
- Home Assistant's best route is resolved again for the next cycle; and
- recovery uses exponential backoff from 1 to 60 seconds with jitter. Its base
  delay is capped at eight seconds while Home Assistant still has an
  advertisement no more than ten seconds old, producing an approximately
  6.4-to-9.6-second jittered wait at the ceiling; and
- during normal recovery, a newly received connectable advertisement or queued
  user command wakes the current backoff early. Advertisement-triggered recovery
  keeps a one-second settling cooldown;
- a per-purifier circuit breaker covers both H7124 and H7129. It opens only after
  three unstable cycles and two advertisement-triggered wakes occur within two
  minutes. The third failure gets a minimum five-second post-cleanup delay and
  later failures get eight seconds. Advertisements and queued commands cannot
  bypass that active floor, but shutdown remains immediate; and
- the circuit resets after the purifier remains ready for 30 seconds. H7129
  negotiation failures retain their `e7-01`/`e7-02` detail, while H7124 records
  its applicable connection, subscription, or initialization stage.

The first runtime connection attempt can accept a cached connectable
advertisement no more than five seconds old. After a failed route, retry requires
newer advertisement evidence. Each advertisement wait is bounded to ten seconds,
each GATT connector cycle to 45 seconds, and explicit Add Device validation to
five minutes. A connector cycle permits up to three low-level attempts against
the selected route before full cleanup, fresh-advertisement admission, and outer
backoff. Individual validation cycles remain debug-only while that window is
active; only the final failure is returned to the setup flow.

Previously configured entries do not wait for a first connection while Home
Assistant is starting. Their entities load immediately as unavailable and the
same connection owner recovers in the background. This prevents an unplugged or
out-of-range purifier from consuming Home Assistant's global bootstrap timeout.
Successful essential initialization automatically publishes current state and
makes the entities available.

Backend GATT calls are bounded independently so a connected-but-stalled BlueZ,
adapter, or proxy operation cannot freeze the owner task. Notification
subscription has a 15-second deadline, command writes have a 10-second deadline,
and best-effort disconnect cleanup has a five-second deadline. Timed-out backend
tasks are cancelled, retained until cancellation completes, and observed so they
cannot produce orphaned-task warnings.

After a purifier has connected successfully at least once, a dropped link marks
its entities unavailable without generating a coordinator error for every
expected recovery cycle. Cached values are not presented as currently available.
Recovery continues indefinitely, and a successful initialization restores the
entities and publishes the newly authoritative state.

Once GATT and the plaintext or encrypted application channel are healthy, every
startup request receives three response attempts. An exhausted capability,
metadata, air-quality, or night-light request is retained in diagnostics and the
sweep continues without discarding the connection. If the essential `aa 01`
device-state request remains silent, the client retries it in up to three
three-attempt batches separated by short delays. If all nine attempts remain
silent, the connected session is recycled. An actual disconnect, GATT/channel
failure, or invalid H7129 session also forces a new connection and session.
Isolated protected frames that fail validation are discarded; continued absence
of the required response is handled by the same bounded request policy.

The documented H7129 `ee aa` refresh uses the same three attempts per request.
An exhausted secondary refresh response is recorded and the remaining refresh
continues on the current session. Because `aa 01` is the connection's essential
health signal, three missing `aa 01` refresh responses trigger reconnection.
Refresh telemetry is lower priority than user controls. If a command arrives
during a refresh transaction, the refresh yields without waiting for the rest of
that transaction's timeout budget. After the command finishes, the sweep resumes
at the interrupted request so requests are neither skipped nor reordered.

Commands are serialized and have a 120-second end-to-end deadline. Up to three
bounded sends are permitted. Response silence is retried immediately on the
existing application session, avoiding a full reconnect between each send. A
transport failure enters normal recovery while preserving the absolute command.
After an ambiguous timeout or disconnect, initialization first re-queries
authoritative state and suppresses a replay if the requested state is already
confirmed. Newer pending controls of the same type supersede older ones, and
commands are never replayed after their deadline or beyond their send budget.
If a command remains unconfirmed, its final Home Assistant error retains the
requested plaintext frame, each send's connection generation and timing, the
three response-timeout summaries with ignored-frame samples, and any `ee 05`
physical update or `3a 05` command echo observed during or after the
transaction. This bounded evidence survives the recovery reconnect that
follows an ambiguous write.

Fan controls complete when the purifier returns the exact 20-byte `3a 05`
command frame. This acknowledgement immediately publishes the requested mode
and prevents unnecessary retries or reconnects. A different `3a 05` frame is
ignored. Unsolicited `ee 05` frames continue to report physical fan-mode changes
and may replace the acknowledged mode at any time. H7124 matches these frames
directly; H7129 applies the same rules after decrypting them.

Startup and reconnect initialization restore current fan mode without adding a
new poll. Only an `aa 05` response matched to its active selector request can
change state. H7124 resolves all six modes from `aa 05 00`; H7129 resolves
Auto/Sleep/Turbo there and waits for the matched `aa 05 01` manual level before
publishing Low/Medium/High. Unknown combinations remain unknown. The same logic
is reused by the documented H7129 `ee aa` refresh sweep.

Cleanup runs before config-entry setup, before a new connection, after a failed
connection cycle, during shutdown/unload, and after entry removal. Removal does
not request Bluetooth rediscovery because automatic discovery is disabled.

## State and protocol limitations

- Fan mode is restored by the matched startup `aa 05` requests, confirmed after
  a Home Assistant command by the exact `3a 05` acknowledgement, and superseded
  by unsolicited `ee 05` physical updates. It becomes unknown during
  reconnection until the new startup responses resolve it. If a secondary mode
  query remains silent, availability is preserved with fan mode unknown.
- Some H7129 RGB responses acknowledge a query or command without proving the
  colour currently displayed. The last authoritative colour is retained, and
  ambiguous RGB commands are not treated as safely reconciled from cached state.
- Physical brightness and RGB notification payloads are not fully documented.
- PM2.5 wire values above 999 are treated as unavailable sentinels.
- **Near** and **Far** are relative labels based on one scan's RSSI. They do not
  guarantee that a GATT connection will succeed.

See [the protocol document](docs/govee-ble-air-purifier-protocol.md) for the
trace-supported wire findings.

## Troubleshooting

### No devices found

- Confirm the purifier is powered on.
- Disconnect it from the Govee app and close the app.
- Move the Home Assistant adapter or Bluetooth proxy closer.
- Start a new **Add device** flow. Each flow runs a new ten-second scan.
- Confirm the advertised name begins with `GVH7124` or `ihoment_H7129_`.

### Bluetooth devices were seen without a supported purifier name

The scan received fresh connectable advertisements, but Home Assistant did not
have a valid H7124 or H7129 name for those addresses. Move the purifier or
Bluetooth proxy closer and start a new **Add device** flow. A later scan can use
a valid name retained by Home Assistant or learned during an earlier setup flow,
but it still requires the purifier's address to be freshly observed. The
integration does not infer a purifier model from its Bluetooth address and does
not connect to unidentified devices to read their names.

### Device was listed but is no longer visible

The purifier advertised during the list scan but did not produce another fresh
advertisement during the selected-address check. This happens before a GATT
connection is attempted. Retry after checking power, range, and the Govee app.

### Unable to connect

Fresh advertising proves only that attempting a connection is reasonable. It
does not prove service discovery will complete. Check the logged route, RSSI,
advertisement age, proxy slots, connection stage, and stale-connection cleanup
result. A purifier at roughly -75 dBm or weaker may advertise successfully while
GATT remains unreliable.

Add Device validation retries for up to five minutes. Intermediate connection
failures are retained in debug diagnostics instead of appearing as coordinator
errors. If setup ultimately reports `cannot_connect`, every bounded attempt in
that five-minute window failed or the purifier stopped advertising. An already
configured purifier instead loads as unavailable and keeps recovering in the
background without delaying Home Assistant startup.

### Physical changes are not reflected

Confirm that the integration still owns the BLE connection. Another central,
including the Govee app, can displace or block Home Assistant. Enable debug
logging and look for notification RX, disconnect, and recovery messages.

## Logs and diagnostics

Connection and protocol failures appear in Home Assistant's normal log. Error
details can include:

- the client state and connection generation;
- startup requests still incomplete after three response attempts;
- refresh requests still incomplete after three response attempts;
- queued/active command counts and the active command's send-attempt count;
- refresh preemption count, interrupted request, and ordered resume requests;
- the selected and current adapter/proxy route;
- RSSI and advertisement age;
- Home Assistant reachability and connection-slot diagnostics;
- GATT stage and elapsed connection time;
- the active/last GATT operation, its deadline, elapsed time, timeout count, and
  any cancellation task still being observed;
- partial-client and stale-connection cleanup results;
- recovery failure and advertisement-wake counts, failure stage, cycle and
  backoff timing, active circuit floor, wake reason, and cleanup outcome;
- essential initialization batch and wire-attempt counts;
- request name, retry count, received and matched frames; and
- a bounded sample of ignored application frames.

Enable detailed logging in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.govee_ble_air_purifier: debug
    bleak_retry_connector: debug
    homeassistant.components.bluetooth: debug
```

Restart Home Assistant, reproduce the problem once, then open
**Settings > System > Logs**. Debug logging covers setup scan timing and
candidates, route selection, connection stages, characteristic discovery,
notification subscription, transaction matching, disconnects, cleanup, and
recovery. Encryption keys and H7129 negotiation payloads are deliberately not
logged, but decrypted application frames are logged at debug level. Bluetooth
addresses and device-specific metadata should be redacted before sharing logs.

Home Assistant config-entry diagnostics expose redacted entry data, cached
purifier state, connection status, route evidence, transport counters, current
stage, cleanup statistics, recent bounded failures, recovery-circuit state and
timing, and startup fan-mode fragments (including the selector-01 value),
resolution, and connection generation.

## Removal and updates

Remove a purifier from **Settings > Devices & services**. The integration shuts
down its runtime connection and performs best-effort address-level cleanup. A
later setup requires opening **Add device** again; there is no automatic
rediscovery card.

During an integration reload, Home Assistant shutdown, or update, the owner task
is cancelled, outstanding commands fail cleanly, the active client is
disconnected, H7129 session state is erased, and stale address-level cleanup is
attempted. After a host crash or power loss, the next setup performs defensive
cleanup before reconnecting.

## Developer guide

### Architecture

| Module | Responsibility |
| --- | --- |
| `config_flow.py` | Manual scan window, name/model classification, selection freshness, validation |
| `bluetooth.py` | Home Assistant scanner/cache adapter, route diagnostics, GATT transport, connection cleanup |
| `channel.py` | Plaintext H7124 channel and per-connection H7129 negotiation/encryption |
| `frame.py` / `crypto.py` | Frame validation, checksum, and cryptographic transforms |
| `protocol.py` / `models.py` | Typed commands, events, response matching, model-specific request sequences |
| `client.py` | Single connection owner, initialization, notifications, polling, command queue, recovery |
| `coordinator.py` | Cached push state and Home Assistant availability/error propagation |
| `fan.py`, `light.py`, `sensor.py` | Entity mappings only; no direct Bluetooth I/O |
| `diagnostics.py` | Redacted config-entry and runtime diagnostics |

The dependency direction is deliberate: Home Assistant entities call the
coordinator, the coordinator calls the reliable client, the client composes a
protocol with a channel, and the channel uses the GATT transport. Protocol and
models remain independent of Home Assistant and Bluetooth.

### Local development

Create a Python environment with the project dependencies, then run:

```bash
env PYTHONPATH=. .venv/bin/ruff check .
env PYTHONPATH=. .venv/bin/pytest -q
```

The tests cover setup scanning and cache freshness, separate address-level
reachability and model/name identity, Home Assistant and session name retention,
connection timeouts and cleanup, stale callback generations, H7129 session
negotiation, request/response matching, notification decoding, command recovery,
entities, diagnostics-related lifecycle behavior, and trace extraction.

### PacketLogger trace extraction

Apple PacketLogger `.pklg` files can be reduced to purifier advertisements,
connection setup, GATT discovery, and ATT traffic with the included extractor:

```bash
python3 scripts/extract_air_purifier_trace.py \
  --model h7129 \
  --address 5C:E7:53:F9:6A:7D \
  --output h7129-extract.txt \
  "/path/to/H7129 Negotiation.pklg"
```

The capture is read-only. Output retains original record numbers, absolute and
connection-relative timestamps, direction, connection handle, L2CAP channel,
decoded ATT operation, and raw bytes.

## References

- [Phased implementation plans](docs/plans/README.md)
- [Govee purifier protocol](docs/govee-ble-air-purifier-protocol.md)
- [Home Assistant Bluetooth expectations, APIs, and reliable handling](docs/home-assistant-bluetooth-expectations-and-api.md)
- [Home Assistant integration and HACS reference](docs/home-assistant-bluetooth-integration-reference.md)
- [License](LICENSE)

Release documentation reflects integration version 0.3.26.
