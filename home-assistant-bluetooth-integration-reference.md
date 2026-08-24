# Home Assistant Bluetooth Integration and HACS Distribution Reference

Compiled from official Home Assistant and HACS documentation. Retrieved 2026-08-23.

This reference describes documented structures, APIs, lifecycle behavior, registry behavior, validation rules, and distribution procedures. It does not describe a particular implementation.

## 1. Integration model and file layout

A Home Assistant integration has a permanent, unique domain. The integration directory name and the `domain` value in `manifest.json` must match. A custom integration is loaded from `<config directory>/custom_components/<domain>`; a core integration is loaded from `homeassistant/components/<domain>`.

The documented minimum integration directory contains:

```text
<domain>/
├── __init__.py
└── manifest.json
```

Files commonly added for a device integration are:

```text
custom_components/<domain>/
├── __init__.py              # integration setup, config-entry setup and unload
├── manifest.json            # metadata, dependencies, requirements, discovery
├── config_flow.py           # UI setup and discovery flows
├── const.py                 # domain and shared constants
├── coordinator.py           # coordinator subclass, when one is defined
├── entity.py                # shared base entity, when one is defined
├── sensor.py                # sensor entity platform
├── binary_sensor.py         # binary-sensor entity platform
├── switch.py                # switch entity platform
├── button.py                # button entity platform
├── number.py                # number entity platform
├── select.py                # select entity platform
├── services.yaml            # descriptions for integration service actions
├── strings.json             # config-flow, entity, error, repair, and action strings
├── diagnostics.py           # downloadable config-entry/device diagnostics
├── repairs.py               # repair flows, when supplied
├── quality_scale.yaml       # quality-scale rule status for core integrations
└── brand/                   # custom-integration branding assets
```

Entity platforms are split by Home Assistant entity domain: `sensor.py` supplies sensor entities, `switch.py` supplies switch entities, and so on. Integration-wide setup belongs in `__init__.py`. Home Assistant's file-structure documentation names `coordinator.py` for a `DataUpdateCoordinator` subclass and `entity.py` for a shared base entity.

Sources: [integration file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/), [creating the first integration](https://developers.home-assistant.io/docs/creating_component_index/), [common modules rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/common-modules/).

## 2. `manifest.json`

Every integration requires `manifest.json`. A custom integration also requires a `version` recognized by AwesomeVersion, such as a valid SemVer or CalVer value. Core integrations omit `version`.

The Home Assistant manifest fields directly relevant to a Bluetooth custom integration are:

| Key | Documented meaning |
| --- | --- |
| `domain` | Permanent integration identifier; must match the directory name. |
| `name` | Display name. |
| `version` | Required for custom integrations; omitted by core integrations. |
| `codeowners` | GitHub usernames or teams responsible for the integration. |
| `documentation` | URL for integration documentation. |
| `issue_tracker` | URL where custom-integration issues are reported. |
| `integration_type` | Main focus: `device`, `hub`, `service`, `helper`, `entity`, `hardware`, `system`, or `virtual`; `virtual` is not available to custom integrations. |
| `iot_class` | Communication and update classification, such as local polling or local push. |
| `config_flow` | Set to `true` when `config_flow.py` creates config entries. |
| `single_config_entry` | When `true`, Home Assistant permits only one config entry for the domain. |
| `dependencies` | Integrations that must be set up successfully before this integration loads. |
| `after_dependencies` | Optional integrations that are loaded first when present. |
| `requirements` | Pinned Python package requirements. Custom integrations include only requirements not already required by Core. |
| `loggers` | Logger names used by package requirements. |
| `bluetooth` | One or more Bluetooth discovery matcher objects. |

HACS additionally requires these manifest keys for an integration repository: `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, and `version`.

Documented structural example:

```json
{
  "domain": "example_ble",
  "name": "Example BLE",
  "version": "1.0.0",
  "codeowners": ["@github-user"],
  "config_flow": true,
  "dependencies": ["bluetooth_adapters"],
  "documentation": "https://example.com/documentation",
  "integration_type": "device",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/github-user/example-ble/issues",
  "requirements": ["example-ble-library==1.0.0"],
  "bluetooth": [
    {
      "service_data_uuid": "0000fd3d-0000-1000-8000-00805f9b34fb",
      "connectable": false
    }
  ]
}
```

The values above illustrate the documented schema; the domain, URLs, dependency, package, IoT class, integration type, matcher, and connectability value are determined by the integration's actual behavior.

Sources: [integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/), [HACS integration requirements](https://hacs.xyz/docs/publish/integration/).

## 3. Bluetooth infrastructure and terminology

Home Assistant's Bluetooth integration centralizes local adapters and remote adapters. Both are called scanners. A Bluetooth proxy is a remote networked adapter. An advertisement is a broadcast that can be received without connecting. A connection is an active two-way link and requires a scanner capable of outgoing connections.

The Bluetooth integration detects nearby devices, and discoveries appear under Settings > Devices & services. Scanner state, capabilities, connections, and advertisements are shown under Settings > Bluetooth.

The developer documentation specifies the following behavior for integrations consuming Bluetooth:

- An integration that needs a Bluetooth adapter lists `bluetooth_adapters` in `manifest.json` dependencies. A manual-only integration that directly calls the shared Bluetooth APIs without manifest discovery matchers also lists `bluetooth`, ensuring that the manager is loaded before its user flow scans. These dependencies cause supported remote adapters to be connected before the integration attempts to use them.
- `bluetooth.async_get_scanner(hass)` returns Home Assistant's shared `BleakScanner` wrapper. It avoids creating an additional scanner and remains valid when adapter settings change.
- `connectable=True` is the default for Bluetooth matching and APIs. It limits results to scanners capable of making outgoing connections.
- `connectable=False` receives data from both connectable and non-connectable scanners. It is the documented setting for devices that only require advertisements.
- A newly created `BleakClient` is used for each connection rather than retaining a client between connections.
- Connection timeouts are at least 10 seconds because BlueZ may need to resolve GATT services on a first or changed connection.
- Transient Bluetooth connection errors and first-attempt failures are expected by the documented connection model. The Home Assistant Bluetooth documentation identifies `bleak-retry-connector` as the package for connection establishment and retry handling.

Home Assistant's user documentation describes three scanner modes:

| Mode | Documented behavior |
| --- | --- |
| `AUTO` | Usually listens passively and requests short active-scan windows when an integration or device needs additional data. |
| `ACTIVE` | Continuously requests full advertisement information. |
| `PASSIVE` | Only listens and never requests extra advertisement information. |

Sources: [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth), [Bluetooth developer overview](https://developers.home-assistant.io/docs/bluetooth/), [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/).

## 4. Bluetooth discovery matching

Bluetooth discovery matchers are declared in the manifest's `bluetooth` array. A matcher may use:

| Matcher field | Meaning |
| --- | --- |
| `connectable` | Whether the discovery must arrive through a scanner capable of connecting. |
| `local_name` | Advertised local name, with Unix-style pattern matching. Patterns are not permitted in the first three characters. |
| `service_uuid` | Advertised service UUID. |
| `service_data_uuid` | UUID used as a service-data key. |
| `manufacturer_id` | Bluetooth manufacturer/company identifier. |
| `manufacturer_data_start` | Starting bytes of manufacturer data, expressed as integers from 0 through 255. |

All fields in one matcher object must match. The integration is discovered when any matcher object in the array matches.

Examples from the documented matcher forms:

```json
{
  "bluetooth": [
    { "local_name": "Prodigio_*" }
  ]
}
```

```json
{
  "bluetooth": [
    { "service_uuid": "cba20d00-224d-11e6-9fb8-0002a5d5c51b" }
  ]
}
```

```json
{
  "bluetooth": [
    {
      "manufacturer_id": 76,
      "manufacturer_data_start": [6]
    }
  ]
}
```

A 16-bit Bluetooth UUID is expanded into the Bluetooth Base UUID. For example, `0xfd3d` becomes `0000fd3d-0000-1000-8000-00805f9b34fb`.

When a matcher is found and the Bluetooth integration is loaded, Home Assistant starts the integration's `bluetooth` config-flow step. Duplicate and already-configured filtering is performed by the config flow, not by the manifest matcher.

Sources: [manifest Bluetooth matching](https://developers.home-assistant.io/docs/creating_integration_manifest/#bluetooth), [config flow](https://developers.home-assistant.io/docs/core/integration/config_flow/).

## 5. UI setup, discovery, and adding devices

### 5.1 Config flow files and data

UI configuration requires:

- `"config_flow": true` in `manifest.json`.
- `config_flow.py` in the integration directory.
- A class derived from `homeassistant.config_entries.ConfigFlow` with the integration domain passed in the class declaration.
- User-visible flow text in `strings.json` under the `config` key.

Config-flow step methods are named `async_step_<step_id>`. Reserved step IDs include `user`, `bluetooth`, `reauth`, `reconfigure`, and `import`.

Home Assistant's quality-scale rule separates stored values as follows:

- Data required to establish the connection belongs in `ConfigEntry.data`.
- Non-connection settings belong in `ConfigEntry.options`.
- A `reconfigure` step changes required setup data.
- An options flow changes optional settings.
- A `reauth` step replaces invalid or expired authentication data.

### 5.2 Bluetooth discovery flow

The documented discovery sequence is:

1. A manifest Bluetooth matcher matches a `BluetoothServiceInfoBleak` discovery.
2. Home Assistant invokes `async_step_bluetooth(discovery_info)`.
3. The flow assigns a stable unique ID with `async_set_unique_id(...)`.
4. The flow prevents a duplicate flow and aborts when the same device is already configured, normally with `_abort_if_unique_id_configured()`.
5. The discovery data is validated and retained for the confirmation step.
6. The discovery step presents a confirmation to the user. A discovery step does not directly finish by creating a config entry.
7. After confirmation, `async_create_entry(title=..., data=...)` creates the config entry.
8. Home Assistant calls the integration's `async_setup_entry` for the new entry.

A unique ID is required when a dedicated `bluetooth` discovery step is implemented. It must be a stable string within the integration domain and cannot be user-changeable. Documented acceptable sources include a serial number, a MAC address obtained from the device API or discovery handler and normalized with `homeassistant.helpers.device_registry.format_mac`, or another identifier permanently stored on the device. IP addresses, configurable device names, and user-changeable hostnames are not acceptable config-entry unique IDs.

If a dedicated discovery step is omitted, discovery may be routed to the `user` step. The documented built-in discoverable flow for unauthenticated integrations supports manifest discovery protocols, checks that devices can be discovered before completion, and permits one config entry whose setup discovers all available devices.

### 5.3 Manual user flow

`async_step_user(user_input)` is invoked when a user selects Add integration. The documented flow pattern displays a form when input is absent; when input is present, it validates connectivity or credentials, assigns and checks a stable unique ID, and creates the config entry only after validation succeeds.

### 5.4 Devices found after initial setup

For a config entry representing multiple devices, Home Assistant's dynamic-device quality rule calls for newly appearing devices to generate their relevant entities after initial setup. The documented coordinator pattern compares current device identifiers to a set of known identifiers on every coordinator update and passes entities for new identifiers to `async_add_entities(...)`.

For a config entry representing one discovered Bluetooth device, each new unmatched device can start its own Bluetooth discovery flow and, after confirmation, create its own config entry.

When an integration uses manifest-driven automatic discovery, removal can call `bluetooth.async_rediscover_address(hass, address)` to clear match history and immediately replay cached discovery so the device can be offered again without a Home Assistant restart. A manual-only integration should omit this call and let the next explicit user flow perform its own scan. `async_clear_address_from_match_history` only clears history for a future advertisement; it does not immediately replay discovery.

Sources: [config flow](https://developers.home-assistant.io/docs/core/integration/config_flow/), [config-flow quality rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow/), [dynamic devices](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/dynamic-devices/), [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/).

## 6. Config-entry runtime lifecycle

Config entries are persistent configuration records created through the UI. During startup, Home Assistant first runs normal integration setup and then calls `async_setup_entry(hass, entry)` for each config entry. It also calls `async_setup_entry` immediately for an entry created while Home Assistant is running.

The documented platform lifecycle has these elements:

```python
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up a config entry."""
    entry.runtime_data = runtime_object
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

`ConfigEntry.runtime_data` stores non-persistent runtime objects such as a Bluetooth parser, client, or coordinator. A typed config-entry alias can declare the runtime-data type. Runtime data is tied to the config-entry lifecycle and is cleaned up when the entry unloads.

Every forwarded entity platform implements its own `async_setup_entry(hass, entry, async_add_entities)` and calls `async_add_entities(...)` with the entities supplied by that platform.

Unload handling removes entities, unsubscribes event listeners, stops coordinators, and closes active connections. `entry.async_on_unload(cancel_callback)` associates a callback with config-entry unload. An entity-specific subscription is registered from `async_added_to_hass` and removed from `async_will_remove_from_hass`.

Sources: [config entries](https://developers.home-assistant.io/docs/config_entries_index/), [`runtime_data` rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/), [fetching data](https://developers.home-assistant.io/docs/integration_fetching_data/).

## 7. Receiving Bluetooth data

Home Assistant documents the following coordinator selection:

| Device communication pattern | Coordinator |
| --- | --- |
| Advertisements are the primary update method; primary functions are sensors, binary sensors, or events; no connection is needed | `PassiveBluetoothProcessorCoordinator` |
| Advertisements are primary; primary functions are sensors, binary sensors, or events; some values require a connection | `ActiveBluetoothProcessorCoordinator` |
| Advertisements are primary; entity types extend beyond sensors, binary sensors, and events; no connection is needed | `PassiveBluetoothCoordinator` / `PassiveBluetoothDataUpdateCoordinator` |
| Advertisements are primary; entity types extend beyond sensors, binary sensors, and events; an active connection is needed | `ActiveBluetoothDataUpdateCoordinator` |
| The device communicates only through active connections and does not use advertisements for updates | `DataUpdateCoordinator` |

Processor coordinators parse `BluetoothServiceInfoBleak` updates and distribute processed data to sensor or binary-sensor processors. Active processor/data coordinators evaluate `needs_poll_method` when advertisement data changes and call `poll_method` when an active GATT read is required.

A Bluetooth coordinator is normally started only after its entity platforms have subscribed. The documented setup pattern forwards platform setup first and then registers `coordinator.async_start()` with `entry.async_on_unload(...)`.

The Bluetooth manager provides these consumer APIs:

| API | Documented function |
| --- | --- |
| `async_register_callback` | Subscribe to matching advertisement changes; returns a cancellation callback. Supports manifest matcher fields plus `address`. |
| `async_get_scanner` | Return the shared `BleakScanner` wrapper. |
| `async_scanner_count` | Count available scanners, optionally restricted by connectability. |
| `async_ble_device_from_address` | Return a `BLEDevice` from the nearest reachable scanner of the requested connectability; returns `None` when unavailable. |
| `async_last_service_info` | Return the latest `BluetoothServiceInfoBleak` from the scanner with the best RSSI for the requested connectability. |
| `async_address_present` | Report whether an address is currently present. |
| `async_track_unavailable` | Register a callback for loss of visibility; detection can take up to five minutes. |
| `async_discovered_service_info` | Return cached discoveries that are still present. |
| `async_process_advertisements` | Await advertisements until a supplied predicate succeeds or the timeout expires. |
| `async_request_active_scan` | Run a one-shot active sweep on scanners configured in `AUTO` mode. |
| `async_rediscover_address` | Clear match history and immediately retrigger discovery from cached data. |
| `async_address_reachability_diagnostics` | Produce a human-readable connection/advertisement reachability explanation; its text is not a stable machine-readable format. |

When a callback is first registered, cached advertisements are replayed. `BluetoothCallbackReplay` selects `OLDEST_FIRST` (default), `NEWEST_FIRST`, or `DISABLED`.

Home Assistant suppresses a repeated advertisement when manufacturer data, service data, service UUIDs, and name all match the previously delivered advertisement for the same address. `async_clear_advertisement_history` clears only this packet-deduplication state; it does not clear integration discovery-match history.

Sources: [fetching Bluetooth data](https://developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data/), [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/).

## 8. Entities, devices, and registries

### 8.1 Entity role

An entity is a Home Assistant data point or control. The platform module derives entities from the domain-specific class, such as `SensorEntity`, `BinarySensorEntity`, `SwitchEntity`, or `ButtonEntity`.

Entity properties are read when state is written. They return cached in-memory values and do not perform network, Bluetooth, file, or other I/O. Data is fetched through a coordinator, `async_update`, or a push subscription.

Important generic entity properties include:

| Property | Registry/state meaning |
| --- | --- |
| `unique_id` | Stable identifier within the integration/platform combination; enables entity-registry registration. |
| `device_info` | Links or creates a device-registry record when the entity belongs to a config entry and has a unique ID. |
| `has_entity_name` | Must be `True` for new integrations. |
| `name` / `translation_key` | Entity's data-point name; the device name is not included in the entity name. |
| `available` | Whether Home Assistant can read or control the underlying data point. |
| `should_poll` | Defaults to `True`; set to `False` for coordinator or subscription-driven entities. |
| `device_class` | Standard semantic classification for the entity domain. |
| `supported_features` | Bit flags for optional features defined by the entity domain. |
| `entity_category` | `CONFIG` for a configuration control or `DIAGNOSTIC` for diagnostic information. |
| `entity_registry_enabled_default` | Whether a new registry entity is enabled by default. |
| `extra_state_attributes` | Additional information that explains the current state; static metadata belongs in device info, and distinct changing measurements are represented as separate sensors. |

### 8.2 Entity unique IDs

An entity with a `unique_id` is entered into the entity registry. The registry lookup key is the entity platform, integration domain, and entity unique ID. Therefore, the entity unique ID does not include the platform name or integration domain.

For a physical device exposing multiple entities, the documented pattern combines the stable device identifier with a stable entity-specific suffix, for example `<device-id>-temperature` and `<device-id>-humidity`. Unique IDs cannot be configurable or changed by the user. A config-entry ID is a last-resort entity unique ID when no stable device or service identifier exists.

Entity-registry membership preserves the entity ID across restarts and allows the user to rename, disable, or otherwise customize the entity without changing the integration's stable identity.

### 8.3 Device registry

A Home Assistant device represents a physical device with its own control unit or a service. One device can contain multiple entities. A child or endpoint can identify a routing parent with `via_device`.

Automatic device registration occurs from an entity's `device_info` only when:

- The entity is loaded through a config entry.
- The entity has a non-`None` unique ID.

The device registry matches device information using `identifiers` and `connections`. `identifiers` are sets of `(DOMAIN, external_identifier)` tuples. `connections` are sets of connection-type/identifier tuples, such as a normalized MAC address connection. Other device information includes `name`, `manufacturer`, `model`, `model_id`, `serial_number`, `sw_version`, `hw_version`, `configuration_url`, `suggested_area`, and `via_device`.

### 8.4 Naming

For new integrations, `has_entity_name` is `True`. The entity name identifies only the data point, not the device or area. Home Assistant combines device and entity names for display and initial entity-ID generation.

- A main entity for a device has `name = None`; its display name is the device name.
- A secondary entity uses its own translated data-point name, such as `Battery` or `Temperature`; Home Assistant combines it with the device name.
- Hard-coded natural-language names are replaced by `translation_key` entries in `strings.json`. Proper nouns, product models, and names supplied by a device or library are not translated.

### 8.5 Updates and availability

Polling entities implement `update()` or `async_update()` and leave `should_poll=True`. Push or coordinator-driven entities set `should_poll=False` and schedule state writes when data changes. `CoordinatorEntity` receives coordinator updates and includes coordinator availability behavior.

When no data can be fetched from a device or service, the entity is marked unavailable. When a successful update contains no value for one particular data point, its state is unknown rather than making every entity unavailable.

Sources: [entity model](https://developers.home-assistant.io/docs/core/entity/), [entity registry](https://developers.home-assistant.io/docs/entity_registry_index/), [device registry](https://developers.home-assistant.io/docs/device_registry_index/), [entity unavailable rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/).

## 9. Sensor-specific state

A sensor derives from `SensorEntity` and is read-only. Its core properties are:

| Property | Meaning |
| --- | --- |
| `native_value` | Required native value: string, number, date, datetime, Decimal, or `None`, subject to device-class restrictions. |
| `native_unit_of_measurement` | Unit in which the device or library supplies the native value. |
| `device_class` | Standard type such as temperature, humidity, battery, signal strength, power, or energy; a class can restrict value type and units. |
| `state_class` | Statistical meaning for numeric sensors. |
| `options` | Allowed states for an enum sensor; cannot be combined with a state class or native unit. |
| `suggested_display_precision` | Suggested decimal display precision. |

The state classes are:

- `MEASUREMENT`: a present-time measurement; Home Assistant records hourly minimum, maximum, and mean statistics.
- `MEASUREMENT_ANGLE`: a present-time angle measurement.
- `TOTAL`: a total that can increase or decrease; Home Assistant records accumulated change.
- `TOTAL_INCREASING`: a monotonically increasing positive total that can periodically reset to zero.

Long-term statistics require a valid state class and compatible device class/unit. Current measurements such as temperature, humidity, and power use `MEASUREMENT`; a remaining battery percentage is also a measurement rather than a total.

Source: [sensor entity](https://developers.home-assistant.io/docs/core/entity/sensor/).

## 10. Translations and user-visible strings

Backend strings are placed in `strings.json` beside the integration code. Supported top-level categories include:

- `title`
- `config`
- `options`
- `entity`
- `device`
- `services`
- `triggers`
- `conditions`
- `exceptions`
- `issues`
- `selectors`
- `common`

Config-flow forms, field descriptions, errors, and abort reasons are under `config`. Entity names and translated enum states are under `entity`, keyed first by platform and then by the entity's `translation_key`. Repair issue text is under `issues`. Integration action descriptions are represented in `services.yaml`, with translatable strings under `services` when applicable.

During Home Assistant Core development, `python3 -m script.translations develop` compiles `strings.json` changes for local display. `python3 -m script.hassfest` activates and validates config-flow and generated integration data.

Source: [backend localization](https://developers.home-assistant.io/docs/internationalization/core/), [config-flow translations](https://developers.home-assistant.io/docs/core/integration/config_flow/#translations).

## 11. Failure handling and resource cleanup

The documented config-entry failure behavior is:

| Condition | Home Assistant mechanism |
| --- | --- |
| Device, dependency, or service temporarily unavailable during integration setup | Raise `ConfigEntryNotReady` from the integration-level `async_setup_entry`; Home Assistant retries with increasing intervals. |
| Credentials invalid or expired | Raise `ConfigEntryAuthFailed` from integration-level setup or a `DataUpdateCoordinator`; Home Assistant starts reauthentication. |
| Coordinator update communication failure | Raise `UpdateFailed`; coordinator entities become unavailable. |
| Service/action input invalid | Raise `ServiceValidationError`. |
| Service/action execution or communication failure | Raise `HomeAssistantError`. |

`ConfigEntryNotReady` raised from an entity platform's `async_setup_entry` is too late for config-entry retry handling; setup validation and its exception occur in the integration-level `__init__.py` setup.

On unload, integration resources are cleaned up: entities are unloaded, advertisement or event callbacks are cancelled, coordinator listeners stop, and open connections close. Push subscriptions tied to individual entities are created in `async_added_to_hass` and cancelled in `async_will_remove_from_hass`.

Sources: [handling setup failures](https://developers.home-assistant.io/docs/integration_setup_failures/), [config entries](https://developers.home-assistant.io/docs/config_entries_index/), [action exceptions rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-exceptions/).

## 12. Async execution and external libraries

Home Assistant's core runs on an asyncio event loop. Calls to Home Assistant APIs from a coroutine use their `async_` variants. Entity properties do not perform I/O.

Blocking functions are executed outside the event loop with `await hass.async_add_executor_job(...)`; blocking library code can use the event loop executor. Network, file, sleep, subprocess, import, and CPU-heavy operations block the event loop when invoked synchronously inside a coroutine.

Python libraries declared in `requirements` are installed by Home Assistant. Requirements use pip-compatible pinned strings. A custom integration includes only packages not already present in Home Assistant Core requirements. Home Assistant's highest quality tier requires an asynchronous dependency and strict typing, but those are quality-scale rules rather than HACS repository validity rules.

Sources: [working with async](https://developers.home-assistant.io/docs/asyncio_working_with_async/), [blocking operations](https://developers.home-assistant.io/docs/asyncio_blocking_operations/), [manifest requirements](https://developers.home-assistant.io/docs/creating_integration_manifest/#requirements).

## 13. Diagnostics and repairs

An integration can provide config-entry diagnostics and per-device diagnostics in `diagnostics.py`. Users download config-entry diagnostics from the config-entry options menu and device diagnostics from the device page. If device-specific diagnostics are absent, the device page supplies config-entry diagnostics.

Diagnostics do not expose passwords, API keys, authentication tokens, location data, or personal information. Home Assistant supplies `async_redact_data` for removing sensitive keys from diagnostic output.

Repairs are persistent or runtime issues displayed to users. An integration creates an issue through the issue registry with a domain, unique issue ID, severity, translation key, persistence choice, and an optional repair-flow or information URL. Severity values are `WARNING` for a future break requiring attention, `ERROR` for something currently broken, and `CRITICAL` for true panic conditions.

Sources: [integration diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/), [repairs](https://developers.home-assistant.io/docs/core/platform/repairs/).

## 14. Tests and automated validation

Home Assistant's documented integration tests operate through public Home Assistant interfaces:

- Set up config entries through `hass.config_entries.async_setup`.
- Assert state through `hass.states`.
- Call actions through `hass.services`.
- Assert device records through the device registry.
- Assert entity records through the entity registry.
- Assert config-entry state through `ConfigEntry.state`.
- Use `MockConfigEntry` for test entries.

Config-flow test coverage is a Bronze quality-scale rule. The full integration quality-scale test threshold at Silver is above 95 percent. Core integration tests are run with `pytest`; Home Assistant's documented targeted coverage command is:

```shell
pytest ./tests/components/<domain>/ \
  --cov=homeassistant.components.<domain> \
  --cov-report term-missing -vv
```

Hassfest validates integration metadata, translations, services, and generated data. HACS provides a separate GitHub Action that runs HACS's own repository validator.

Sources: [testing Home Assistant](https://developers.home-assistant.io/docs/development_testing/), [integration quality-scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/), [HACS validation action](https://hacs.xyz/docs/publish/action/).

## 15. HACS repository format

HACS supports public GitHub repositories. The repository has a GitHub description, GitHub topics, a README describing use, and a root-level `hacs.json`.

For an integration repository, HACS requires one integration under `custom_components` and all runtime files inside that integration directory:

```text
repository-root/
├── custom_components/
│   └── <domain>/
│       ├── __init__.py
│       ├── manifest.json
│       ├── config_flow.py
│       ├── strings.json
│       ├── sensor.py
│       └── ...
├── .github/
│   └── workflows/
│       └── validate.yml      # HACS validation workflow; required for default-catalog submission
├── hacs.json
└── README.md
```

There can be only one subdirectory under `custom_components`. If multiple integration directories exist, HACS manages only the first. The alternate `content_in_root: true` layout tells HACS that distributable content is at repository root.

The HACS integration requirements specify branding through Home Assistant Brands. Current default-repository validation checks for a local integration brand directory containing at least `icon.png`, then falls back to the matching domain in `home-assistant/brands`.

Sources: [HACS general publishing requirements](https://hacs.xyz/docs/publish/start/), [HACS integration requirements](https://hacs.xyz/docs/publish/integration/), [HACS default inclusion](https://hacs.xyz/docs/publish/include/).

## 16. `hacs.json`

`hacs.json` is located at repository root. `name` is the only required field documented for this file.

| Key | Type | Meaning |
| --- | --- | --- |
| `name` | string | Display name in HACS. Required. |
| `content_in_root` | boolean | Repository content is at root rather than in a subdirectory. |
| `zip_release` | boolean | Integration content is distributed as a release ZIP; requires `filename`. |
| `filename` | string | File HACS locates for single-file types or `zip_release`. |
| `hide_default_branch` | boolean | Default branch is not offered as a downloadable version. |
| `country` | string or list | ISO 3166-1 alpha-2 country restriction. |
| `homeassistant` | string | Minimum Home Assistant version. |
| `hacs` | string | Minimum HACS version. |
| `persistent_directory` | string | Relative integration directory retained across upgrades. |

Minimal form:

```json
{
  "name": "Example BLE"
}
```

Version-constrained form:

```json
{
  "name": "Example BLE",
  "homeassistant": "2026.8.0",
  "hacs": "2.0.0"
}
```

Appending `b0` to the Home Assistant version permits beta releases when HACS compares compatibility. Without `b0`, only official Home Assistant releases satisfy that version expression.

Source: [HACS general publishing requirements](https://hacs.xyz/docs/publish/start/#hacsjson).

## 17. HACS releases and versions

GitHub releases are optional for a valid HACS integration repository. When releases exist, HACS shows the five newest releases together with the default branch unless the default branch is hidden. When no releases exist, HACS installs the repository's default branch.

For repositories using releases, HACS uses the GitHub release tag as the remote version. A Git tag by itself is not a release. For repositories without releases, HACS uses the first seven characters of the latest commit hash as the remote version.

The `version` inside `custom_components/<domain>/manifest.json` remains required for every custom integration regardless of whether GitHub releases are used.

Sources: [HACS integration releases](https://hacs.xyz/docs/publish/integration/#github-releases-optional), [HACS versions](https://hacs.xyz/docs/publish/start/#versions), [Home Assistant manifest version](https://developers.home-assistant.io/docs/creating_integration_manifest/#version).

## 18. HACS validation workflow

HACS documents this GitHub workflow:

```yaml
name: Validate

on:
  push:
  pull_request:
  schedule:
    - cron: "0 0 * * *"
  workflow_dispatch:

permissions: {}

jobs:
  validate-hacs:
    runs-on: ubuntu-latest
    steps:
      - name: HACS validation
        uses: hacs/action@main
        with:
          category: integration
```

The HACS action runs the same validation code used by HACS. On a release-based repository it checks the latest release; otherwise it checks the default branch. On pull requests it checks the submitted fork/branch. The action's `category` input is `integration` for a custom component.

The action supports ignored checks, but HACS default-repository submission requires the HACS action to pass without errors or ignored checks. Default submission also requires Hassfest to pass for an integration.

Source: [HACS GitHub Action](https://hacs.xyz/docs/publish/action/), [HACS default inclusion](https://hacs.xyz/docs/publish/include/).

## 19. HACS distribution paths

### 19.1 Custom repository distribution

A repository with the recognized HACS integration structure can be added directly by a HACS user:

1. Open HACS.
2. Open the top-right menu.
3. Select Custom repositories.
4. Enter the public GitHub repository URL.
5. Select the integration repository type.
6. Select Add.
7. Open the repository in HACS and select Download.
8. Restart Home Assistant when HACS marks the download as pending restart.
9. Add the integration from Settings > Devices & services through its config flow.

This path does not add the repository to the default HACS catalog. HACS still validates the repository structure.

### 19.2 Default HACS catalog inclusion

The documented default-catalog submission requirements are:

- The submitter is the repository owner or a major contributor.
- The repository is public and hosted on GitHub.
- It can already be added to HACS as a custom repository.
- The HACS Action passes without errors or ignored checks.
- Hassfest passes for the integration.
- A full GitHub release, not only a tag, is created after those actions pass.
- The repository is active and not archived.
- GitHub issues are enabled.
- The repository has a description and topics.
- The integration manifest and `hacs.json` validate.
- Branding validates.
- A country-limited integration declares `country` in the released `hacs.json`.
- The pull request adds the repository alphabetically to the `integration` file in `hacs/default`.
- The pull request is editable and is made from a separate branch created from `master`, not directly from the fork's `master` branch.
- The pull-request template is completed.

Custom integrations that override core integrations or serve as alpha/beta testing versions of core integrations are not accepted into the default catalog, though HACS permits them as custom repositories.

Sources: [HACS custom repositories](https://hacs.xyz/docs/faq/custom_repositories/), [using the HACS dashboard](https://hacs.xyz/docs/use/repositories/dashboard/), [default repository inclusion](https://hacs.xyz/docs/publish/include/).

## 20. Integration documentation format

Home Assistant's documented integration-page structure is:

1. Introduction
2. Use cases
3. Supported and unsupported devices
4. Prerequisites
5. Configuration
6. Configuration options
7. Supported functionality, including entities and platforms
8. Triggers
9. Conditions
10. Actions
11. Examples
12. Data updates
13. Known limitations
14. Troubleshooting
15. Community notes
16. Removal instructions

HACS requires a repository README with use information and renders that README in the repository details view. The README therefore supplies HACS-facing installation and usage information, while `manifest.json` supplies the documentation and issue-tracker URLs.

Sources: [integration page structure](https://developers.home-assistant.io/docs/documenting/integration-docs-examples/), [HACS README requirement](https://hacs.xyz/docs/publish/start/#readme), [documented supported functionality](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/docs-supported-functions/).

## 21. Quality-scale rule index

The Home Assistant Integration Quality Scale is a core-integration grading framework, not a HACS repository validity standard. The current tiers are cumulative.

Bluetooth device integrations intersect with the following documented rules:

### Bronze baseline

- UI config flow.
- Full config-flow test coverage.
- Unique config entry.
- Connection test during config flow.
- Setup validation.
- Stable entity unique IDs.
- `has_entity_name=True`.
- Runtime objects in `ConfigEntry.runtime_data`.
- Correct entity event subscription lifecycle.
- Appropriate polling interval, when polling.
- Service actions registered during integration setup, when supplied.
- Branding and baseline documentation.

### Silver reliability

- Config-entry unload support.
- Entities become unavailable when communication fails.
- Reauthentication flow, when authentication exists.
- One unavailable log transition and one recovery transition rather than repeated log messages.
- Defined request parallelism.
- Active integration ownership.
- Above 95 percent test coverage.

### Gold functionality and user interface

- Device-registry records.
- Automatic discovery where the device supports it.
- Discovery updates for changed connection information.
- Devices added after setup create entities.
- Stale devices are removed when absence is certain.
- Diagnostics.
- Reconfiguration flow.
- Repair issues for conditions requiring user action.
- Entity categories, device classes, translated names, translated exceptions, and translated icons.
- Less common or high-churn entities disabled by default.
- Documentation of update behavior, functionality, examples, supported devices, limitations, and troubleshooting.

### Platinum implementation

- Async dependency.
- Strict typing.
- Web-session injection where HTTP is used.

Sources: [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/), [quality-scale rule list](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/).

## 22. Official source index

### Home Assistant developer documentation

- [Creating the first integration](https://developers.home-assistant.io/docs/creating_component_index/)
- [Integration file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [Integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Config flow](https://developers.home-assistant.io/docs/core/integration/config_flow/)
- [Config entries](https://developers.home-assistant.io/docs/config_entries_index/)
- [Bluetooth overview](https://developers.home-assistant.io/docs/bluetooth/)
- [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/)
- [Fetching Bluetooth data](https://developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data/)
- [Fetching data and coordinators](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Entity model](https://developers.home-assistant.io/docs/core/entity/)
- [Sensor entity](https://developers.home-assistant.io/docs/core/entity/sensor/)
- [Entity registry](https://developers.home-assistant.io/docs/entity_registry_index/)
- [Device registry](https://developers.home-assistant.io/docs/device_registry_index/)
- [Backend localization](https://developers.home-assistant.io/docs/internationalization/core/)
- [Handling setup failures](https://developers.home-assistant.io/docs/integration_setup_failures/)
- [Integration diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/)
- [Repairs](https://developers.home-assistant.io/docs/core/platform/repairs/)
- [Working with async](https://developers.home-assistant.io/docs/asyncio_working_with_async/)
- [Blocking operations](https://developers.home-assistant.io/docs/asyncio_blocking_operations/)
- [Testing](https://developers.home-assistant.io/docs/development_testing/)
- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
- [Integration quality-scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)
- [Integration documentation structure](https://developers.home-assistant.io/docs/documenting/integration-docs-examples/)

### Home Assistant user documentation

- [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth)

### HACS documentation

- [Publisher documentation](https://hacs.xyz/docs/publish/)
- [General publishing requirements and `hacs.json`](https://hacs.xyz/docs/publish/start/)
- [Integration repository requirements](https://hacs.xyz/docs/publish/integration/)
- [HACS validation action](https://hacs.xyz/docs/publish/action/)
- [Custom repositories](https://hacs.xyz/docs/faq/custom_repositories/)
- [Default repository inclusion](https://hacs.xyz/docs/publish/include/)
- [Repository dashboard and downloads](https://hacs.xyz/docs/use/repositories/dashboard/)
