# Govee BLE Air Purifier

A Home Assistant custom integration for local control of Govee H7124 and H7129
air purifiers over Bluetooth. H7124 uses plaintext application frames. H7129
negotiates a fresh encrypted session for every Bluetooth connection.

## Supported devices

- Govee H7124
- Govee H7129

## Features

- Purifier power
- Low, medium, and high manual fan speeds
- Auto, Sleep, and Turbo fan presets
- Night-light power, brightness, and RGB colour
- PM2.5 and filter-life sensors
- Physical-control updates received through Bluetooth notifications
- Automatic reconnection and bounded command recovery for unreliable Bluetooth
  links

## Installation with HACS

1. Add this repository to HACS as a custom repository with the **Integration**
   category.
2. Download **Govee BLE Air Purifier**.
3. Restart Home Assistant.
4. Home Assistant should discover nearby supported purifiers automatically. You
   can also go to **Settings > Devices & services > Add integration**, select
   **Govee BLE Air Purifier**, and choose a discovered purifier from the list.

The purifier must be visible to a connectable Home Assistant Bluetooth adapter
or Bluetooth proxy during setup. Close the Govee app before setup because the
purifier permits only one Bluetooth central connection at a time.

The integration matches the observed advertised-name families `GVH7124*` and
`ihoment_H7129_*`; the model is inferred from that name. Manual setup never asks
for a Bluetooth address. It lists unconfigured discoveries by advertised name,
labels the strongest current signal **Near**, and labels the remaining devices
**Far**. The address is retained internally only as the stable device identity.

## Data updates

On each connection, the integration subscribes to notifications and reproduces
the protocol's response-paced startup initialization. H7129 session negotiation
happens before that sweep. During normal operation, only the documented
`aa 01` device-state query is polled, on the official fixed three-second cadence.
For H7124, the first idle poll preserves the captured 1.936-second post-startup
gap; subsequent requests use the fixed write-to-write cadence.
PM2.5, filter life, fan mode, and night-light state otherwise come from startup,
refresh sweeps, and unsolicited device notifications.

If Bluetooth drops or the purifier is unplugged, the integration marks state
unavailable, discards any H7129 session material, and reconnects with capped
exponential backoff. A command interrupted by a disconnect is verified after a
new connection when the protocol exposes authoritative state; old commands are
not replayed indefinitely.

## Known limitations

- The official protocol exposes no authoritative fan-mode query. Fan mode is
  known after the integration sets it or receives an `ee 05` notification.
- Some H7129 RGB responses acknowledge receipt without proving the colour being
  displayed. The last usable colour is retained.
- Physical brightness and RGB notification payloads are not fully documented.
- A purifier connected to the Govee app cannot simultaneously connect to Home
  Assistant.

## Removal

Remove the purifier under **Settings > Devices & services**, then remove the
integration from HACS if it is no longer needed.
