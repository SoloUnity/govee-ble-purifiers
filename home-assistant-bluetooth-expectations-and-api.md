# Home Assistant Bluetooth Expectations, APIs, and Reliable Handling

Practical engineering reference for Home Assistant Bluetooth integrations.
Verified against the Home Assistant developer documentation on 2026-08-23.

This document focuses on Bluetooth behavior: scanner ownership, discovery,
advertisement delivery, route selection, GATT connections, lifecycle,
availability, recovery, diagnostics, and testing. It does not define any device
protocol. Protocol-specific framing, encryption, commands, and timings belong in
a separate protocol document.

For broader integration structure, entity conventions, HACS packaging, and
quality-scale requirements, see
[`home-assistant-bluetooth-integration-reference.md`](home-assistant-bluetooth-integration-reference.md).

## 1. Three kinds of statements

The distinction matters because Home Assistant APIs can change while a device's
wire protocol remains constant.

- **Home Assistant contract** means behavior documented by Home Assistant.
- **Observed implementation behavior** means behavior visible in current Home
  Assistant, Bleak, BlueZ, or `bleak-retry-connector`, but not necessarily a
  permanent public contract.
- **Engineering recommendation** means a reliability policy an integration
  should adopt. It is not a requirement imposed by Home Assistant.

Code should depend on documented contracts wherever possible. Diagnostics may
inspect implementation behavior, but business logic must not parse unstable
human-readable diagnostic strings or assume private timeout values.

## 2. Responsibility boundaries

A reliable integration separates four responsibilities.

| Layer | Owns |
| --- | --- |
| Home Assistant Bluetooth | Local adapters, remote proxies, scanning, advertisement cache, route selection, reachability, and connection-slot information |
| BLE transport | One GATT connection, service discovery, characteristic lookup, notification subscription, writes, disconnect callbacks, and cleanup |
| Device channel | Plaintext or encrypted session establishment and transformation of complete device frames |
| Device protocol | Requests, responses, unsolicited notifications, state decoding, polling rules, and command semantics |

Home Assistant should select the adapter or proxy. The transport should not
create its own scanner or permanently bind itself to the adapter that first
discovered the device. Encryption belongs above GATT so the same transport can
support both plaintext and encrypted devices.

## 3. Choose the correct Home Assistant update model

Home Assistant supplies different coordinator patterns based on how data is
obtained.

| Device behavior | Suitable model |
| --- | --- |
| All state arrives in advertisements; primarily sensors or events | `PassiveBluetoothProcessorCoordinator` |
| Advertisements drive updates, with occasional active reads; primarily sensors or events | `ActiveBluetoothProcessorCoordinator` |
| Advertisement-driven device with non-sensor entities | `PassiveBluetoothCoordinator` |
| Advertisement-driven device that sometimes connects | `ActiveBluetoothDataUpdateCoordinator` |
| Device communicates primarily through a persistent or integration-managed GATT connection | A `DataUpdateCoordinator` or purpose-built coordinator and client |

These frameworks are useful, but they do not replace a device-specific session
state machine when the device requires ordered initialization, encryption
negotiation, unsolicited notifications, serialized commands, or continuous
connection ownership.

## 4. Manifest expectations

An integration that needs access to local or remote Bluetooth adapters should
declare:

```json
{
  "dependencies": ["bluetooth_adapters"]
}
```

This allows supported remote adapters to be ready before the integration tries
to use them.

Bluetooth discovery matchers belong in the manifest's `bluetooth` array. A
matcher can use:

- `connectable`
- `local_name`
- `service_uuid`
- `service_data_uuid`
- `manufacturer_id`
- `manufacturer_data_start`

All fields in one matcher must match. Any one matcher in the array may trigger
discovery. Local-name values use pattern matching, so `Product_*` means the
literal prefix followed by any sequence of characters.

Set `connectable: true` when the device requires an outgoing GATT connection.
Set `connectable: false` when advertisements are sufficient; this permits data
from both connectable and listening-only controllers. `connectable` defaults to
`true` in the APIs, but it should still be made explicit where it communicates
intent.

## 5. Scanner ownership and routes

### 5.1 Home Assistant owns scanning

Do not start an independent Bleak scanner. If a library needs a scanner, obtain
Home Assistant's shared wrapper:

```python
scanner = bluetooth.async_get_scanner(hass)
```

The shared scanner avoids radio and process overhead and continues working when
the user changes adapter settings.

### 5.2 A device can have multiple routes

The same address may be visible through a local adapter and several remote
proxies. Each route can have different RSSI, age, connectability, and available
connection slots. The best route can change at any time.

Resolve a connectable `BLEDevice` immediately before connecting:

```python
device = bluetooth.async_ble_device_from_address(
    hass,
    address,
    connectable=True,
)
```

The API returns the device from the nearest configured adapter that can reach
the address, or `None` when no suitable route is available. A stored
`BLEDevice` is a route snapshot, not a permanent device handle. Re-resolve it
for a later connection attempt.

### 5.3 Connection slots are finite

Local adapters and proxies can limit simultaneous connections. A route being
visible does not prove it currently has a free connection slot. Home
Assistant's reachability diagnostics include route visibility and slot
allocation. Integrations should treat slot exhaustion as transient and avoid
creating competing connection attempts.

## 6. Advertisement delivery semantics

### 6.1 Callback registration

Register a callback through Home Assistant:

```python
cancel = bluetooth.async_register_callback(
    hass,
    callback,
    {"address": address, "connectable": True},
    bluetooth.BluetoothScanningMode.PASSIVE,
)
```

The returned callable removes the registration. Store it in runtime state or
register it with `entry.async_on_unload()`.

The callback receives `BluetoothServiceInfoBleak` plus a Bluetooth change
value. It runs on Home Assistant's event loop and should remain quick; schedule
longer asynchronous work instead of blocking inside it.

### 6.2 Cached replay is the default

When a callback is registered, Home Assistant normally replays cached
advertisements. Replay policy is controlled by `BluetoothCallbackReplay`:

| Value | Meaning |
| --- | --- |
| `OLDEST_FIRST` | Default; replay cached advertisements in first-seen order |
| `NEWEST_FIRST` | Replay the newest cached advertisement first |
| `DISABLED` | Deliver only future live callback events |

Use `DISABLED` when callback arrival itself is being used as a wake-up or
freshness signal:

```python
cancel = bluetooth.async_register_callback(
    hass,
    callback,
    {"address": address, "connectable": True},
    bluetooth.BluetoothScanningMode.PASSIVE,
    replay=bluetooth.BluetoothCallbackReplay.DISABLED,
)
```

Without this setting, a callback that fires immediately after registration may
contain old cache data.

### 6.3 Identical advertisements are deduplicated

Home Assistant reduces load by suppressing an advertisement when its name,
manufacturer data, service data, and service UUIDs match the previous packet
from that address. RSSI or timestamp changes alone do not guarantee another
callback.

Consequences:

- Callback age is not necessarily the age of the most recent radio packet.
- Lack of repeated callbacks does not prove the device stopped advertising.
- `async_last_service_info()` may contain a newer timestamp or RSSI than the
  integration's last callback.
- A device that emits a static wake-up advertisement needs explicit history
  clearing before the next identical packet can trigger a callback.

Clear only advertisement deduplication state with:

```python
bluetooth.async_clear_advertisement_history(hass, address)
```

This does not clear integration discovery-match history.

### 6.4 Freshness is a policy, not callback timing

When a protocol specifically requires a live wake-up advertisement:

1. Record a monotonic cutoff.
2. Disable registration replay.
3. Clear advertisement history before waiting.
4. Accept only callback data delivered after the cutoff.
5. Resolve a new connectable `BLEDevice` after that event.
6. Apply a bounded wait and report the cached and current routes on timeout.

If the protocol does not require a live wake-up packet, prefer a bounded
freshness policy over clearing shared history. A recent cached route can be
used for the first attempt; remember its advertisement timestamp and require a
newer timestamp or live callback before retrying. This tolerates callback
deduplication without repeatedly selecting the same failed route.

### 6.5 Scan modes

| Mode | Integration expectation |
| --- | --- |
| `PASSIVE` | Listen without requesting extra active-scan data |
| `ACTIVE` | Request active-scan information |
| User scanner `AUTO` mode | Normally passive; Home Assistant schedules active windows when requested |

For one-shot discovery, `await bluetooth.async_request_active_scan(hass)` asks
`AUTO` scanners to perform a shared active window. It does not override scanners
the user explicitly placed in `PASSIVE` or `ACTIVE` mode.

`async_process_advertisements()` is the higher-level API for waiting until an
advertisement satisfies a predicate. When called with a specific address and a
non-passive mode, its timeout also controls the requested active-scan window.

## 7. API map

These are the principal consumer-facing APIs. Treat signatures as versioned
Home Assistant interfaces and consult the current developer documentation when
supporting multiple Home Assistant releases.

### Discovery and subscriptions

| API | Use |
| --- | --- |
| `async_register_callback()` | Subscribe to matching advertisement changes; returns an unsubscribe callable |
| `async_process_advertisements()` | Wait for a matching advertisement that passes a predicate |
| `async_request_active_scan()` | Trigger a one-shot active window on `AUTO` scanners |
| `async_track_unavailable()` | Receive a delayed callback when the address is no longer considered present |

### Route and cache lookup

| API | Use |
| --- | --- |
| `async_ble_device_from_address()` | Resolve the current best `BLEDevice`, optionally requiring a connectable route |
| `async_last_service_info()` | Obtain the latest service information from the best route of the requested type |
| `async_address_present()` | Check whether the address is presently in Home Assistant's Bluetooth cache |
| `async_discovered_service_info()` | List discoveries Home Assistant still considers present |
| `async_scanner_devices_by_address()` | Inspect every adapter/proxy route that sees one address |

### Diagnostics and scanner inspection

| API | Use |
| --- | --- |
| `async_address_reachability_diagnostics()` | Produce a human-readable explanation of route, scanner, and slot conditions |
| `async_scanner_count()` | Check whether scanners of the required type exist |
| `async_current_scanners()` | Inspect active scanners for diagnostics only |
| `async_scanner_by_source()` | Inspect the scanner associated with one source |
| `async_get_scanner()` | Obtain Home Assistant's shared Bleak scanner wrapper |

Do not parse the text returned by reachability diagnostics. Its wording is not
stable. Log or display it for humans.

Do not modify scanner objects returned by inspection APIs or retain references
to them beyond immediate inspection.

### History and rediscovery

| API | Use |
| --- | --- |
| `async_clear_advertisement_history()` | Make the next identical advertisement eligible for callback delivery |
| `async_clear_address_from_match_history()` | Allow a future advertisement to trigger discovery again without immediately replaying cached data |
| `async_rediscover_address()` | Clear discovery history and immediately reconsider cached data |

Call `async_rediscover_address()` when a config entry or managed Bluetooth
device is removed so it can be offered for setup again without restarting Home
Assistant.

## 8. GATT connection expectations

### 8.1 Use a new client per connection

Home Assistant explicitly recommends against reusing a `BleakClient` between
connections. Construct a fresh client for each attempt or let
`bleak-retry-connector` do so. Reusing a disconnected client can retain stale
backend, service-cache, or adapter state.

### 8.2 Allow service discovery time

Home Assistant recommends a connection timeout of at least 10 seconds because
BlueZ may need to resolve services on a first connection or after services
change. A timeout shorter than this can cancel valid work.

That minimum is not an instruction to wait indefinitely. A higher-level
integration may impose a bounded attempt deadline, provided it is at least 10
seconds and partial-client cleanup is reliable.

### 8.3 Expect transient failure

The first connection attempt may fail even when:

- the advertisement is fresh;
- RSSI appears adequate;
- a connectable route exists; and
- the scanner reports a free slot.

Use `bleak-retry-connector` for normalized exceptions, service-cache handling,
and transient retry behavior. Decide whether retries occur inside one connector
call or in the integration's outer state machine. Do not accidentally stack
both into an unbounded retry tree.

For route-sensitive or wake-up-sensitive devices, one bounded connector attempt
per outer cycle is usually easier to reason about:

1. Wait for or select a route.
2. Create one client.
3. Attempt connection within a deadline.
4. Clean up completely on failure.
5. Back off.
6. Re-resolve the route and create a new client.

### 8.4 Serialize connection ownership

Only one task should connect, disconnect, negotiate, initialize, and operate a
specific peripheral. Use one owner task plus a command queue. Entities should
submit commands to that owner rather than touching Bleak directly.

This prevents:

- two entities competing for the same peripheral;
- overlapping service discovery and writes;
- disconnecting a client another operation still uses;
- concurrent encrypted-session negotiation; and
- exhausting adapter or proxy connection slots.

## 9. Recommended connection state machine

```text
STOPPED
   |
   v
WAIT_FOR_ADVERTISEMENT --timeout--> BACKOFF
   |
   v
CONNECTING -------------failure----> CLEANUP --> BACKOFF
   |
   v
SUBSCRIBING ------------failure----> CLEANUP --> BACKOFF
   |
   v
NEGOTIATING ------------failure----> CLEANUP --> BACKOFF
   |
   v
INITIALIZING -----------failure----> CLEANUP --> BACKOFF
   |
   v
READY ----------------disconnect---> CLEANUP --> BACKOFF
```

Every cycle should have a generation number. Notification and disconnect
callbacks capture the generation and ignore events from older clients. This is
essential because backend callbacks can arrive after cancellation or cleanup.

Backoff should be bounded and normally include jitter. Reset exponential
backoff only after a connection has remained healthy for a meaningful period;
otherwise a connection that succeeds for milliseconds can cause rapid retry
loops.

## 10. Handling poor signals and stuck connections

RSSI is evidence, not a guarantee. It is measured during advertising and may
not represent connection-channel performance. Remote proxies add network and
slot state that RSSI alone cannot describe.

A robust policy should:

- record the selected route, source, RSSI, and advertisement age;
- bound advertisement waits separately from GATT connection attempts;
- bound each connection attempt;
- retain a reference to the client while it is still connecting;
- disconnect that partial client when the attempt is cancelled or times out;
- invalidate callbacks from the failed generation;
- clear advertisement deduplication state when a new live packet is required;
- re-resolve Home Assistant's best route before retrying;
- use capped exponential backoff with jitter; and
- retain a short failure history so the final setup error describes more than
  the last cancellation.

Do not equate `in_progress=1` in reachability diagnostics with slot exhaustion.
It may represent the integration's own connection attempt. Likewise, a fresh
advertisement and free slot only prove that attempting a connection is
reasonable; they do not prove the connection will complete.

## 11. Disconnect handling

Unexpected disconnects include unplugging the peripheral, radio loss,
supervision timeout, proxy loss, adapter reset, and rejection by a device that
allows only one central.

The disconnect callback should immediately:

1. Verify that the callback belongs to the current generation and owned client.
2. Mark the transport disconnected.
3. Invalidate notification and characteristic references.
4. Invalidate any connection-scoped encryption key.
5. Wake the owner task and any transaction waiting for a notification.
6. Mark data unavailable with the meaningful underlying cause.
7. Let the owner task clean up and enter recovery.

Do not wait for a normal transaction timeout after the disconnect callback has
already proved the link is gone.

Disconnect callbacks may be delivered from a backend thread. Use
`loop.call_soon_threadsafe()` to transfer mutation back to Home Assistant's
event loop when required.

## 12. Notifications, requests, and polling

Subscribe to notifications before sending a request that expects a response.
Otherwise a fast response can arrive before the callback is active.

Use one in-flight request unless the device protocol explicitly supports
pipelining. Match responses by protocol content, not merely by receiving the
next notification. Unsolicited physical-control notifications can legally
arrive while a request is pending and should update cached state without
incorrectly completing the request.

Each transaction needs a deadline. On timeout, record:

- request name;
- attempt number;
- received-frame count;
- matched-fragment count;
- ignored-frame count;
- a small redacted sample of ignored frames; and
- whether a disconnect occurred.

Polling policy comes from the device protocol, not from Home Assistant. If a
device sends authoritative notifications for every change, additional polling
may be unnecessary. If the official protocol polls one specific state query,
poll only that query at the observed cadence. A push-capable entity should use
cached state and coordinator callbacks; entities should not initiate Bluetooth
reads from their property methods.

## 13. Availability semantics

Advertisement availability and GATT availability are different.

For an advertisement-only sensor, `async_track_unavailable()` and learned
advertising intervals are appropriate. Home Assistant notes that unavailable
callbacks may take up to five minutes.

For a connected device:

- a peripheral may stop advertising while connected;
- advertisement presence does not mean the GATT link is healthy;
- advertisement absence does not necessarily mean a healthy owned connection
  is unavailable; and
- the disconnected callback and transaction failures are the primary runtime
  link signals.

Use advertisement presence to decide whether a new connection is plausible,
not as the sole availability definition for an already connected device.

## 14. Config-flow and config-entry lifecycle

### 14.1 Discovery flow

A manifest match invokes `async_step_bluetooth()`. A discovery flow should:

1. Validate the discovery and connectability.
2. Derive a stable unique ID.
3. Abort duplicate flows and already configured devices.
4. Preserve the discovery information for confirmation.
5. Ask the user to confirm before creating an entry.

Do not ask users to type a Bluetooth address when Home Assistant already knows
the supported devices. Device names are labels, not stable unique IDs.

### 14.2 Setup failure

Temporary failures such as an unplugged device or unavailable Bluetooth route
should raise `ConfigEntryNotReady` from the integration's top-level
`async_setup_entry()`. Home Assistant will retry setup. Do not require a Home
Assistant restart to recover from a temporary Bluetooth failure.

Allow the original exception to remain the cause and include a concise message
so the UI and logs retain useful context. Home Assistant handles retry logging;
avoid emitting repeated warning/error messages for the same expected retry.

### 14.3 Runtime storage and unload

Store the coordinator or client in `ConfigEntry.runtime_data`. On unload:

- unload entity platforms;
- cancel advertisement and unavailability callbacks;
- cancel and await owner/background tasks;
- cancel and await child queue/event waits;
- invalidate the protocol channel and session key;
- disconnect the active or partially connecting client; and
- fail outstanding commands with a clear stopped/unavailable error.

Cleanup must be idempotent because setup failure, reload, removal, and Home
Assistant shutdown can reach it through different paths.

## 15. Command recovery

Control commands need bounded end-to-end deadlines. A poor signal must not
leave an entity service call waiting indefinitely.

After a disconnect during a write, the result is ambiguous: the peripheral may
have applied the command before the link failed. On reconnect:

- perform normal initialization first;
- query authoritative state when the protocol exposes it;
- suppress a retry if the desired state is already confirmed;
- retry only idempotent or absolute commands; and
- never replay a command after its user-facing deadline.

Do not infer success from a local write call alone. For write-without-response,
successful handoff to the OS is not device acknowledgement.

## 16. Diagnostics and logging

Normal Home Assistant error logs should summarize failures without requiring
debug logging. Recommended fields are:

- device name and address;
- client status and connection generation;
- connection-attempt number and elapsed time;
- selected and current route source;
- RSSI, advertisement age, and callback age;
- reachability diagnostic text;
- current GATT stage;
- whether a partial client exists;
- successful-connection, TX, and RX counters;
- pre-return and unexpected disconnect details;
- active protocol request;
- recent connection-failure history; and
- the full exception cause chain, including empty `TimeoutError` messages.

Debug logs may additionally include state transitions, advertisement callbacks,
characteristic resolution, notification subscription, transaction matching, and
backoff timing.

Never log encryption keys, credentials, personally identifying payloads, or
unbounded packet history. Home Assistant diagnostics should be redacted and
bounded.

Reachability diagnostics are for humans. Keep them intact in an error message,
but do not branch program behavior by searching their English text.

## 17. Testing expectations

Unit tests should cover at least:

- manifest name matching and model inference;
- connectable and non-connectable filtering;
- replay disabled when live packets are required;
- stale cache rejection;
- identical-advertisement history clearing;
- route re-resolution for each attempt;
- missing route and no-scanner behavior;
- connection timeout and partial-client cleanup;
- multiple failures retained in diagnostics;
- stale-generation notification and disconnect rejection;
- disconnect waking a transaction immediately;
- cancellation of child `Queue.get()` and `Event.wait()` tasks;
- notification subscription before the first request;
- protocol negotiation failure cleanup;
- initialization and response matching;
- unsolicited notification handling;
- command coalescing, deadlines, and ambiguous-write reconciliation;
- setup raising `ConfigEntryNotReady` for transient failures; and
- unload leaving no client, callback, or task behind.

CI should run the Python test suite, Ruff or equivalent linting, JSON validation,
Hassfest, and HACS validation for a custom integration.

## 18. Common mistakes

- Starting a private Bleak scanner instead of using Home Assistant's scanner.
- Treating the first callback after registration as a live advertisement.
- Assuming identical advertisements produce repeated callbacks.
- Reusing one `BLEDevice` or `BleakClient` forever.
- Binding permanently to the first adapter or proxy seen.
- Treating RSSI as proof that GATT will connect.
- Using only a large global timeout, so one stuck attempt consumes all setup
  time.
- Cancelling a connection attempt without retaining and cleaning its partial
  client.
- Allowing entity methods to connect or write concurrently.
- Treating every notification as the pending request's response.
- Keeping an encrypted session key after any disconnect.
- Replaying an ambiguous command without querying state first.
- Using advertisement availability as the sole health signal for a connected
  device.
- Parsing human-readable reachability diagnostics.
- Replacing a detailed failure with a generic `disconnected` message during
  cleanup.

## 19. Policy used by this purifier integration

The Govee purifier integration applies the general model as follows:

- Home Assistant owns scanning and selects local-adapter or proxy routes.
- Callback replay is disabled for connection wake-up waits.
- The first connection accepts a cached connectable advertisement no more than
  five seconds old; a retry requires evidence newer than its previous route.
- Home Assistant's shared advertisement history is not cleared by a connection
  cycle.
- A new connectable `BLEDevice` and new Bleak client are used per cycle.
- A GATT connection attempt has a 15-second deadline.
- A partially connecting client is explicitly disconnected on timeout.
- Local BlueZ connections for the purifier address are closed and verified
  before a new attempt, after a failed attempt, during shutdown, and on removal.
- A surviving address-level connection blocks a new attempt so retries cannot
  consume additional adapter slots.
- The setup window is 60 seconds, permitting at least two full
  advertisement-and-connection attempts under the configured bounds.
- Recovery uses capped exponential backoff with jitter.
- GATT, plaintext/encrypted channel, and purifier protocol are separate layers.
- Notification subscription precedes H7129 negotiation and all application
  requests.
- H7129 session material is discarded on disconnect or negotiation failure.
- Initialization is repeated after every reconnect.
- Only the protocol-defined `aa 01` state query is periodically polled.
- Physical-control notifications update cached state without waiting for a poll.
- Diagnostics retain recent connection failures and Home Assistant reachability
  information.

These values are project policy based on the purifier traces and reliability
goals. They are not universal Home Assistant constants.

## 20. Official references

- [Building a Bluetooth integration](https://developers.home-assistant.io/docs/bluetooth/)
- [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/)
- [Fetching Bluetooth data](https://developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data/)
- [Integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Handling setup failures](https://developers.home-assistant.io/docs/integration_setup_failures/)
- [Config-entry unloading rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-entry-unloading/)
- [Test-before-setup rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-before-setup/)
- [Home Assistant Core Bluetooth source](https://github.com/home-assistant/core/tree/dev/homeassistant/components/bluetooth)
- [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector)
