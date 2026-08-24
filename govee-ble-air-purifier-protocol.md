# Govee BLE Air Purifier Protocol

Protocol reference for Govee H7124 and H7129 air purifiers. All frames in this
document are 20-byte plaintext application frames. H7124 sends them directly;
H7129 encrypts them as described below.

## GATT

| Purpose | UUID |
| --- | --- |
| Service | `00010203-0405-0607-0809-0a0b0c0d1910` |
| Notifications/read | `00010203-0405-0607-0809-0a0b0c0d2b10` |
| Commands/read | `00010203-0405-0607-0809-0a0b0c0d2b11` |

Observed value handles:

| Model | Notification | Command write |
| --- | ---: | ---: |
| H7124 | `0x0012` | `0x0015` |
| H7129 | `0x0016` | `0x0019` |

The observed H7124 notification CCCD is `0x0013`. Discover characteristics and
descriptors by UUID instead of assuming handles are fixed across firmware.

- Subscribe to `...2b10` before starting a transaction.
- Write frames to `...2b11` with ATT Write Command (write without response).
- Only one BLE central can connect to a purifier at a time.

## Official App Polling

While connected, powered on, and otherwise idle, the official Govee app polls
both models with the `aa 01` device-state query every three seconds. The cadence
is fixed from one request write to the next; response timing does not shift the
next poll.

| Model | Wire form | Measured intervals | Response latency |
| --- | --- | --- | --- |
| H7124 | Plaintext `aa 01` | Seven intervals of 3.000 seconds | 69-132 ms; steady-loop median 73 ms |
| H7129 | Session-encrypted `aa 01` | 2.999-3.001 seconds | 56-90 ms; combined median 59 ms |

Steady idle polling does not alternate `aa 19` status or `aa 1b` night-light
queries. H7129 `aa 01` response byte 6 can vary from `02` through `06` while
idle; it is not an authoritative fan-mode field.

## Application Frame

```text
Bytes 0-1   Command or message type
Bytes 2-18  Payload
Byte 19     XOR of bytes 0-18
```

Unused payload bytes are zero unless a command specifies otherwise.

Prefix conventions:

| Prefix | Use |
| --- | --- |
| `aa` | Queries, query responses, and some state notifications |
| `ee` | Unsolicited device notifications |
| `3a` | Controls and corresponding device notifications/echoes |
| `33` | Controls and corresponding device notifications/echoes |
| `e7` | H7129 session negotiation |

## H7129 Encryption

H7129 requires a new encrypted session for every BLE connection. The
16-byte communication key is ASCII `MakingLifeSmarte`.

### Session negotiation

1. Create a checksum-valid `e7 01` frame with random padding, encrypt it with
   the communication key, and write it.
2. Decrypt the device's `e7 01` response with the communication key. Plaintext
   bytes 2-17 are the 16-byte session key.
3. Create a checksum-valid `e7 02` frame with random padding, encrypt it with
   the communication key, and write it.
4. Wait for the device's `e7 02` confirmation under the communication key.
5. Use the session key for all ordinary commands and notifications.

Discard the session key on disconnect or negotiation failure. A delayed
`e7 01` or `e7 02` notification may arrive under the communication key after
negotiation and should not be treated as an application response.

Observed negotiation timing:

| Event | Timing |
| --- | ---: |
| First `e7 01` response after request | 40-117 ms |
| `e7 02` request after the first `e7 01` response | 1-2 ms |
| First `e7 02` confirmation after request | 59-149 ms |
| Duplicate `e7 02` after the first confirmation | 0-29 ms |
| First session-key command after confirmation | 3-5 ms |

Duplicate `e7 01` and `e7 02` notifications can be exact plaintext and wire
duplicates. A duplicate `e7 01` may accompany the first response before the
`e7 02` request or arrive shortly after that request. A delayed duplicate
`e7 02` may arrive after session-key application traffic has begun. Ignore a
duplicate after completing its negotiation step and continue waiting for the
expected application frame under the unchanged deadline.

### Frame transform

Each 20-byte wire frame is transformed independently:

| Bytes | Transform |
| --- | --- |
| 0-15 | AES-128-ECB, one block, no padding |
| 16-19 | XOR with the first four bytes of an RC4-compatible keystream initialized from the same key |

Reinitialize the RC4-compatible state for every frame. Validate the application
checksum after decryption and calculate it before encryption.

## Official App Initialization

The official app performs the same 23-request base initialization sweep on
both models. H7124 begins after enabling notifications and sends plaintext;
H7129 begins after session confirmation and encrypts every application frame.
In one H7124 connection, the first request followed notification subscription
by 3 ms.

The shared base request order is:

```text
33 b2 -> 33 b5
aa 01
aa 05 00 -> aa 05 01 -> aa 05 03
aa 1b 01 -> aa 1b 05
aa 1e 01 02 -> aa 10 -> aa 08 -> aa 26 -> aa 16 -> aa 17 -> aa 19
aa 07 10 -> aa 07 11 -> aa 07 06 -> aa 07 20
aa 1f
ab 01 02 -> ab 01 05 -> ab 01 04
```

H7124 stops after this base sequence. H7129 additionally sends:

```text
ab 02 02 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 aa
```

The H7129 metadata order is not fixed. The additional request has been sent
either 3 ms after the terminating `ab ff` response to `ab 01 04`, or 1 ms
before the `ab 01 04` request. In the latter form, the two metadata transactions
are pipelined and their responses overlap.

| Model | Writes | Notifications | Duration | First-response latency | Pacing |
| --- | ---: | ---: | ---: | ---: | --- |
| H7124 | 23 | 32 | 3.182 s | 59-269 ms; median 119 ms | Strictly response-paced; next request 1-2 ms after the complete response |
| H7129 | 24 | 33 | 1.767-1.825 s | 54-181 ms; per-capture medians 58.5-88 ms | State queries are response-paced; metadata may be pipelined |

For H7124, the first idle `aa 01` poll was sent 1.936 seconds after the final
initialization notification and answered after 69 ms.

### Response completion

The captured sweeps and physical-device observations established these
request-to-response boundaries:

| Request | Response completion |
| --- | --- |
| `33 b2` | One matching response. Captures returned `00` in byte 2; physical H7124 and H7129 devices also returned `01` in byte 2. In both forms, bytes 3-18 were zero. |
| `33 b5` | One matching response with a zero payload |
| `aa 01` | One device-state response |
| `aa 05 00`, `aa 05 01`, `aa 05 03` | One matching `aa 05` response for each subcommand |
| `aa 1b 01`, `aa 1b 05` | One matching night-light state response for each selector |
| H7124 `aa 1e 01 02` | One exact echo |
| H7129 `aa 1e 01 02` | One `aa 1e 03 01` response with bytes 4-18 zero |
| `aa 10`, `aa 08`, `aa 26`, `aa 17`, `aa 07 20`, `aa 1f` | One exact echo for each request |
| `aa 16` | One structured `aa 16` response |
| `aa 19` | One air-quality/status response |
| `aa 07 10`, `aa 07 11`, `aa 07 06` | One matching `aa 07` device-data response for each subcommand |
| `ab 01 02` | One `ab 00` fragment |
| `ab 01 05` | Two fragments: `ab 00`, then `ab ff` |
| `ab 01 04` | Nine fragments: `ab 00`, `ab 01` through `ab 07`, then `ab ff` |
| H7129 `ab 02 02 00 01` | One `ab 00` response containing the `02 02 00 01` selector |

The additional physical H7124 and H7129 `33 b2` response was:

```text
33 b2 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 80
```

The final `80` is the valid XOR checksum for that frame. The H7129 observation
occurred during encrypted startup initialization and was returned once for
each of two `33 b2` request attempts. This establishes that both models can
use the same response shape; the meaning of the `01` value has not been
established.

An H7129 physical device returned this decrypted response once after each of
two encrypted `aa 1e 01 02` startup request attempts:

```text
aa 1e 03 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b6
```

The final `b6` is the valid XOR checksum. This establishes the H7129 response
boundary and shows that, unlike the captured H7124 response, it is not an exact
echo. The meanings of the `03 01` response values have not been established.

Observed H7124 `aa 05` responses begin as follows; remaining payload bytes are
zero through byte 18, followed by the checksum:

```text
aa 05 00 -> aa 05 00 01 03
aa 05 01 -> aa 05 01 03
aa 05 03 -> aa 05 03 00 00 14
```

The `aa 07 10`, `aa 07 11`, and `aa 07 06` responses contain device-specific
binary identifiers. Do not treat their captured payload bytes as model-wide
constants.

### H7129 refresh sweep

An active-session `ee aa` can trigger a shorter refresh sweep beginning with
`33 b5`. The refresh repeats state, mode, night-light, capability, and `aa 19`
queries but omits the `aa 07`, `aa 1f`, and `ab` metadata portion.

## Power

Command type: `33 01`.

| State | Command |
| --- | --- |
| Off | `33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32` |
| On | `33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33` |

The device may echo `33 01`, then send `aa 01`. In `aa 01`, byte 2 is the
applied power state: `00` off or `01` on.

## Fan Mode

Command type: `3a 05`.

| Mode | Model | Command |
| --- | --- | --- |
| Low | Both | `3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f` |
| Medium | Both | `3a 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3c` |
| High | Both | `3a 05 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3d` |
| Sleep | Both | `3a 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3a` |
| Auto Default | H7124 | `3a 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 28` |
| Auto Default | H7129 | `3a 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 2e` |
| Turbo | Both | `3a 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 38` |

Payload layout:

| Byte | Meaning |
| --- | --- |
| 2 | `01` manual, `03` Auto, `05` Sleep, `07` Turbo |
| 3 | Manual level: `01` Low, `02` Medium, `03` High; otherwise `00` |
| 5 | Auto parameter: H7124 `14`, H7129 `12`; otherwise `00` |

Use `3a 05`, not the alternate H7124 `33 05` path. H7129 Quiet and High
Efficiency Auto parameters are unspecified.

## Night Light

Command type: `3a 1b`. Query type: `aa 1b`.

### Power and brightness

Query:

```text
aa 1b 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b0
```

Control payload:

| Byte | Meaning |
| --- | --- |
| 2 | `01` power/brightness selector |
| 3 | `01` power, `02` brightness |
| 4 | Power value (`00` or `01`) or brightness percentage (`01`-`64`, 1-100%) |

Commands:

| Operation | Command |
| --- | --- |
| Power off | `3a 1b 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 21` |
| Power on | `3a 1b 01 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 20` |
| Brightness 1% | `3a 1b 01 02 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 23` |
| Brightness 50% | `3a 1b 01 02 32 00 00 00 00 00 00 00 00 00 00 00 00 00 00 10` |
| Brightness 100% | `3a 1b 01 02 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 46` |

Brightness accepts every whole-number percentage from 1% through 100%.

State responses and notifications use this layout:

```text
<prefix> 1b 01 PP BB 00 00 00 00 00 00 00 00 00 00 00 00 00 00 CS
```

- `prefix` is `aa` after a query, `3a` after a control, or `ee` for an
  unsolicited change.
- `PP` is `00` off or `01` on.
- `BB` is the retained brightness percentage.
- `CS` is the frame checksum.

### RGB color

Query:

```text
aa 1b 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b4
```

RGB control layout:

```text
3a 1b 05 0d RR GG BB 00 00 00 00 00 00 00 00 00 00 00 00 CS
```

Example commands:

| Color | Command |
| --- | --- |
| Red | `3a 1b 05 0d ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 d6` |
| Yellow | `3a 1b 05 0d ff ff 00 00 00 00 00 00 00 00 00 00 00 00 00 29` |
| Green | `3a 1b 05 0d 00 ff 00 00 00 00 00 00 00 00 00 00 00 00 00 d6` |
| Blue | `3a 1b 05 0d 00 00 ff 00 00 00 00 00 00 00 00 00 00 00 00 d6` |

Each RGB component accepts the full `00`-`ff` range, providing the full 24-bit
RGB color spectrum. Both models can return the standard query-response form
`aa 1b 05 0d RR GG BB ... CS`. H7129 can also return
`aa 1b 05 fc 00 00 00 ... 48`; the condition selecting the `fc` form is
unspecified. The `fc` form is a checksum-valid response to an `aa 1b 05` query,
not an unsolicited RGB notification, and contains no usable RGB value. It still
completes the query; retain the previous or default color instead of waiting for
another response.

Observed H7129 `3a 1b 05` color notifications were byte-for-byte copies of the
corresponding controls in both plaintext and ciphertext. Treat them as
acknowledgement echoes rather than independent confirmation of the stored or
displayed color.

## Device State

Query:

```text
aa 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ab
```

Response layout:

| Byte | Meaning |
| --- | --- |
| 2 | Power: `00` off, `01` on |
| 4 | Status flags; normally `81` |
| 6 | H7129 volatile state value; observed `02`-`06`, not authoritative for fan mode |

An `aa 01` frame may also arrive unsolicited. It does not provide authoritative
fan-mode state. On H7129, a physical power change can occur without a preceding
`33 01` command; the app reconciles the change through `aa 01` and refresh
traffic.

## Air Quality and Filter Status

Query:

```text
aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3
```

Response layout:

| Byte | Meaning |
| --- | --- |
| 2 | Status flags; normally `81` |
| 3-4 | PM2.5 as an unsigned big-endian 16-bit integer |
| 5 | Mode-related value; not authoritative |
| 6 | Unspecified |
| 7 | Filter life percentage |
| 8-18 | Unspecified/zero |

Decode PM2.5 as:

```text
raw_pm25 = (byte[3] << 8) | byte[4]
pm25_ug_m3 = raw_pm25 if raw_pm25 <= 999, otherwise unavailable
```

Values above 999, including `ff ff`, are invalid, unavailable, or over-range
sentinels.

## Unsolicited Notifications

### Fan mode: `ee 05`

These notifications can be emitted directly after a physical control change,
without a preceding app `3a 05` command. The payload normally matches the
corresponding command payload, except that observed H7129 Sleep and Turbo
notifications retain `03` in byte 3.

| Mode | Model | Notification |
| --- | --- | --- |
| Low | Both | `ee 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 eb` |
| Medium | Both | `ee 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 e8` |
| High | Both | `ee 05 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 e9` |
| Sleep | H7124 | `ee 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ee` |
| Sleep | H7129 | `ee 05 05 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ed` |
| Auto Default | H7124 | `ee 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 fc` |
| Auto Default | H7129 | `ee 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 fa` |
| Turbo | H7124 | `ee 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ec` |
| Turbo | H7129 | `ee 05 07 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ef` |

On H7129, each observed physical fan-mode notification was followed by an
unsolicited `aa 01` frame 272-331 ms later. Treat `ee 05` as the authoritative
mode update; the precise meaning of the retained H7129 byte-3 value outside
manual mode is unspecified.

### Night-light state: `ee 1b 01`

```text
ee 1b 01 PP BB 00 00 00 00 00 00 00 00 00 00 00 00 00 00 CS
```

`PP` is power and `BB` is retained brightness. This unsolicited layout is
used by both models. Observed H7129 examples are:

```text
ee 1b 01 00 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 90
ee 1b 01 01 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 91
```

These represent off and on while retaining 100% brightness. They can be emitted
after a physical control change without a preceding app command. On H7129, an
unsolicited `aa 01` followed the observed changes after 300-302 ms. Unsolicited
physical brightness and RGB-color payloads remain unspecified.

### Status: `ee 19`

The payload follows the `aa 19` response layout. H7129 can emit `ee 19`
autonomously when sensor/status data changes, without a preceding app request.
One observed update reported PM2.5 `3` and filter life `73%`:

```text
ee 19 81 00 03 01 00 49 00 00 00 00 00 00 00 00 00 00 00 3d
```

An unsolicited `aa 01` followed 299 ms later. Neither notification shifted the
next fixed-rate three-second poll.

On H7124, an `ee 19` update can also be requested with:

```text
33 18 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 2a
```

Use `aa 19` for ordinary polling.

### Connection/refresh: `ee aa`

```text
ee aa 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 44
```

This frame may occur on either model. On H7129 it can arrive under the
communication key before session negotiation or under the session key after
negotiation. It carries no direct application-state payload.

During an active H7129 session, the official app treats `ee aa` as a refresh
trigger. If no request is pending, it begins the shorter response-paced refresh
with `33 b5` about 1 ms later. If a request is pending, it first waits for that
request's matching response; the next request began 62 ms after `ee aa` in the
measured sequence. The frame is not necessarily periodic, and its device-side
trigger is unspecified. No `ee aa` occurred during a separate 75-second
steady-idle interval, so it should not be required as a periodic heartbeat.

## Additional H7124 Queries

These queries are not specified for H7129.

| Query | Purpose |
| --- | --- |
| `aa 06 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ac` | Firmware version |
| `aa 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8b` | Firmware version |
| `aa 07 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ae` | Hardware version/device data |
| `aa 20 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8a` | Hardware version |

`aa 05` is mode-related but is not fully specified. Prefer `ee 05` for mode
changes and `3a 05` for mode control.
