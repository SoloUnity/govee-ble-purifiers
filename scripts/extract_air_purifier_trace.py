#!/usr/bin/env python3
"""Extract Govee air-purifier traffic from Apple PacketLogger traces.

The extractor reads ``.pklg`` files directly, reassembles ACL/L2CAP packets,
identifies purifier connections from their advertised names, GATT UUIDs, and
known characteristic handles, and emits a compact text timeline. Source traces
are never modified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

HCI_COMMAND = 0
HCI_EVENT = 1
ACL_TX = 2
ACL_RX = 3

ATT_CID = 0x0004
LE_SIGNALING_CID = 0x0005
CONNECTION_OPCODES = {0x200D, 0x200E, 0x2043, 0x2085}

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
COMMAND_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
GOVEE_UUIDS_LE = tuple(
    bytes.fromhex(uuid.replace("-", ""))[::-1]
    for uuid in (SERVICE_UUID, NOTIFY_UUID, COMMAND_UUID)
)

MODEL_NAMES = {
    "h7124": ("GVH7124",),
    "h7129": ("ihoment_H7129_",),
}
MODEL_ATTRIBUTE_HANDLES = {
    "h7124": {0x0012, 0x0015},
    "h7129": {0x0016, 0x0019},
}

ATT_OPCODES = {
    0x01: "error_response",
    0x02: "exchange_mtu_request",
    0x03: "exchange_mtu_response",
    0x04: "find_information_request",
    0x05: "find_information_response",
    0x08: "read_by_type_request",
    0x09: "read_by_type_response",
    0x0A: "read_request",
    0x0B: "read_response",
    0x10: "read_by_group_type_request",
    0x11: "read_by_group_type_response",
    0x12: "write_request",
    0x13: "write_response",
    0x1B: "notification",
    0x52: "write_command",
}


@dataclass(frozen=True, slots=True)
class PacketRecord:
    """One PacketLogger record."""

    index: int
    timestamp: float
    packet_type: int
    data: bytes


@dataclass(frozen=True, slots=True)
class L2capSdu:
    """One reassembled L2CAP service data unit."""

    record_indices: tuple[int, ...]
    timestamp: float
    direction: str
    handle: int
    cid: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Advertisement:
    """Relevant fields from one LE Extended Advertising Report."""

    record_index: int
    timestamp: float
    address: str
    name: str | None
    rssi: int | None
    data: bytes


def _packetlogger_endian(data: bytes) -> str:
    """Detect the record-length byte order used by a PacketLogger file."""
    for endian in ("<", ">"):
        length = struct.unpack_from(f"{endian}I", data)[0]
        if 9 <= length <= len(data) - 4:
            return endian
    raise ValueError("not a recognized PacketLogger file")


def read_packetlogger(path: Path) -> list[PacketRecord]:
    """Read all records from an Apple PacketLogger ``.pklg`` file."""
    raw = path.read_bytes()
    if len(raw) < 13:
        raise ValueError(f"{path}: file is too short")
    endian = _packetlogger_endian(raw)
    records: list[PacketRecord] = []
    offset = 0
    while offset < len(raw):
        if offset + 13 > len(raw):
            raise ValueError(f"{path}: truncated record header at byte {offset}")
        length = struct.unpack_from(f"{endian}I", raw, offset)[0]
        end = offset + 4 + length
        if length < 9 or end > len(raw):
            raise ValueError(f"{path}: invalid record length {length} at byte {offset}")
        seconds, microseconds = struct.unpack_from(f"{endian}II", raw, offset + 4)
        records.append(
            PacketRecord(
                index=len(records),
                timestamp=seconds + microseconds / 1_000_000,
                packet_type=raw[offset + 12],
                data=raw[offset + 13 : end],
            )
        )
        offset = end
    return records


def reassemble_l2cap(records: list[PacketRecord]) -> list[L2capSdu]:
    """Reassemble L2CAP SDUs from HCI ACL start and continuation fragments."""
    pending: dict[tuple[int, int], tuple[int, int, bytearray, list[int], float]] = {}
    sdus: list[L2capSdu] = []

    for record in records:
        if record.packet_type not in (ACL_TX, ACL_RX) or len(record.data) < 4:
            continue
        handle_and_flags, acl_length = struct.unpack_from("<HH", record.data)
        handle = handle_and_flags & 0x0FFF
        boundary_flag = (handle_and_flags >> 12) & 0x03
        fragment = record.data[4 : 4 + acl_length]
        key = (record.packet_type, handle)

        if boundary_flag in (0, 2):
            if len(fragment) < 4:
                continue
            expected_length, cid = struct.unpack_from("<HH", fragment)
            payload = bytearray(fragment[4:])
            indices = [record.index]
            started = record.timestamp
        elif boundary_flag == 1 and key in pending:
            expected_length, cid, payload, indices, started = pending.pop(key)
            payload.extend(fragment)
            indices.append(record.index)
        else:
            continue

        if len(payload) < expected_length:
            pending[key] = (expected_length, cid, payload, indices, started)
            continue

        sdus.append(
            L2capSdu(
                record_indices=tuple(indices),
                timestamp=started,
                direction="TX" if record.packet_type == ACL_TX else "RX",
                handle=handle,
                cid=cid,
                payload=bytes(payload[:expected_length]),
            )
        )

    return sdus


def _decode_ad_name(data: bytes) -> str | None:
    offset = 0
    while offset < len(data):
        length = data[offset]
        if length == 0 or offset + 1 + length > len(data):
            break
        ad_type = data[offset + 1]
        value = data[offset + 2 : offset + 1 + length]
        if ad_type in (0x08, 0x09):
            return value.decode("utf-8", errors="replace")
        offset += 1 + length
    return None


def extended_advertisements(record: PacketRecord) -> list[Advertisement]:
    """Decode LE Extended Advertising Reports from one HCI event."""
    data = record.data
    if (
        record.packet_type != HCI_EVENT
        or len(data) < 4
        or data[0] != 0x3E
        or data[2] != 0x0D
    ):
        return []

    reports: list[Advertisement] = []
    offset = 4
    for _ in range(data[3]):
        if offset + 26 > len(data):
            break
        address_bytes = data[offset + 3 : offset + 9]
        rssi_raw = data[offset + 13]
        data_length = data[offset + 23]
        payload_start = offset + 24
        payload_end = payload_start + data_length
        if payload_end > len(data):
            break
        payload = data[payload_start:payload_end]
        reports.append(
            Advertisement(
                record_index=record.index,
                timestamp=record.timestamp,
                address=":".join(f"{value:02X}" for value in address_bytes[::-1]),
                name=_decode_ad_name(payload),
                rssi=struct.unpack("b", bytes((rssi_raw,)))[0],
                data=payload,
            )
        )
        offset = payload_end
    return reports


def _att_attribute_handle(payload: bytes) -> int | None:
    if len(payload) < 3 or payload[0] not in (0x0A, 0x12, 0x1B, 0x52):
        return None
    return int.from_bytes(payload[1:3], "little")


def _models_for_path(path: Path, requested_model: str) -> tuple[str, ...]:
    if requested_model != "auto":
        return (requested_model,)
    upper_name = path.name.upper()
    inferred = tuple(model for model in MODEL_NAMES if model.upper() in upper_name)
    return inferred or tuple(MODEL_NAMES)


def _normalize_address(address: str) -> str:
    compact = "".join(character for character in address if character.isalnum())
    if len(compact) != 12:
        raise ValueError(f"invalid Bluetooth address: {address}")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).upper()


def _is_target_advertisement(
    advertisement: Advertisement,
    models: tuple[str, ...],
    addresses: set[str],
) -> bool:
    if addresses:
        return advertisement.address in addresses
    name = advertisement.name or ""
    return any(
        name.startswith(prefix) for model in models for prefix in MODEL_NAMES[model]
    )


def _target_handles(sdus: list[L2capSdu], models: tuple[str, ...]) -> set[int]:
    attribute_handles = set().union(
        *(MODEL_ATTRIBUTE_HANDLES[model] for model in models)
    )
    handles: set[int] = set()
    for sdu in sdus:
        if sdu.cid != ATT_CID:
            continue
        attribute_handle = _att_attribute_handle(sdu.payload)
        if attribute_handle in attribute_handles or any(
            uuid in sdu.payload for uuid in GOVEE_UUIDS_LE
        ):
            handles.add(sdu.handle)
    return handles


def _connection_addresses(records: list[PacketRecord]) -> dict[int, str]:
    """Map standard LE connection-complete handles to peer addresses."""
    connections: dict[int, str] = {}
    for record in records:
        data = record.data
        if (
            record.packet_type != HCI_EVENT
            or len(data) < 14
            or data[0] != 0x3E
            or data[2] not in (0x01, 0x0A, 0x29)
            or data[3] != 0
        ):
            continue
        handle = int.from_bytes(data[4:6], "little") & 0x0FFF
        connections[handle] = ":".join(f"{value:02X}" for value in data[8:14][::-1])
    return connections


def _target_link_record(record: PacketRecord, handles: set[int]) -> bool:
    """Recognize standard HCI commands/events that explicitly name a handle."""
    data = record.data
    if record.packet_type == HCI_COMMAND and len(data) >= 5:
        opcode = int.from_bytes(data[:2], "little")
        if opcode in (0x0406, 0x041D, 0x2013, 0x2016):
            return int.from_bytes(data[3:5], "little") & 0x0FFF in handles
    if record.packet_type != HCI_EVENT or len(data) < 5:
        return False
    event = data[0]
    if event in (0x05, 0x08, 0x0C):
        return int.from_bytes(data[3:5], "little") & 0x0FFF in handles
    if event == 0x3E and len(data) >= 6 and data[2] in (0x01, 0x0A, 0x29):
        return int.from_bytes(data[4:6], "little") & 0x0FFF in handles
    return False


def _hci_command_opcode(record: PacketRecord) -> int | None:
    if record.packet_type != HCI_COMMAND or len(record.data) < 3:
        return None
    return int.from_bytes(record.data[:2], "little")


def _hci_result_opcode(record: PacketRecord) -> int | None:
    data = record.data
    if record.packet_type != HCI_EVENT or len(data) < 6:
        return None
    if data[0] == 0x0E:
        return int.from_bytes(data[3:5], "little")
    if data[0] == 0x0F:
        return int.from_bytes(data[4:6], "little")
    return None


def _address_on_wire(address: str) -> bytes:
    return bytes.fromhex(address.replace(":", ""))[::-1]


def _hci_description(record: PacketRecord) -> str:
    data = record.data
    opcode = _hci_command_opcode(record)
    if opcode is not None:
        return f"HCI_CMD opcode=0x{opcode:04x} raw={data.hex(' ')}"
    result_opcode = _hci_result_opcode(record)
    if result_opcode is not None:
        event_name = "command_complete" if data[0] == 0x0E else "command_status"
        return (
            f"HCI_EVT {event_name} opcode=0x{result_opcode:04x} " f"raw={data.hex(' ')}"
        )
    if (
        record.packet_type == HCI_EVENT
        and len(data) >= 14
        and data[0] == 0x3E
        and data[2] in (0x01, 0x0A, 0x29)
    ):
        handle = int.from_bytes(data[4:6], "little") & 0x0FFF
        address = ":".join(f"{value:02X}" for value in data[8:14][::-1])
        return (
            f"HCI_EVT le_connection_complete subevent=0x{data[2]:02x} "
            f"status=0x{data[3]:02x} handle=0x{handle:03x} "
            f"address={address} raw={data.hex(' ')}"
        )
    return f"HCI_EVT event=0x{data[0]:02x} raw={data.hex(' ')}"


def _att_description(payload: bytes) -> str:
    if not payload:
        return "empty ATT payload"
    opcode = payload[0]
    description = ATT_OPCODES.get(opcode, f"opcode_0x{opcode:02x}")
    if opcode in (0x02, 0x03) and len(payload) >= 3:
        return f"{description} mtu={int.from_bytes(payload[1:3], 'little')}"
    attribute_handle = _att_attribute_handle(payload)
    if attribute_handle is not None:
        return f"{description} attribute=0x{attribute_handle:04x}"
    return description


def _format_timestamp(timestamp: float, origin: float) -> str:
    instant = dt.datetime.fromtimestamp(timestamp).astimezone()
    return f"{instant.isoformat(timespec='milliseconds')} {timestamp - origin:+9.3f}s"


def _write_line(output: BinaryIO, line: str = "") -> None:
    output.write((line + "\n").encode())


def extract_trace(
    path: Path,
    output: BinaryIO,
    *,
    requested_model: str,
    addresses: set[str],
    context_seconds: float,
) -> None:
    """Write one compact purifier timeline."""
    records = read_packetlogger(path)
    sdus = reassemble_l2cap(records)
    models = _models_for_path(path, requested_model)
    advertisements = [
        advertisement
        for record in records
        for advertisement in extended_advertisements(record)
        if _is_target_advertisement(advertisement, models, addresses)
    ]
    discovered_addresses = {advertisement.address for advertisement in advertisements}
    handles = _target_handles(sdus, models)
    connection_addresses = _connection_addresses(records)
    if addresses and connection_addresses:
        handles = {
            handle
            for handle in handles
            if connection_addresses.get(handle) in addresses
        }
    target_sdus = [sdu for sdu in sdus if sdu.handle in handles]

    if target_sdus:
        origin = target_sdus[0].timestamp
        setup_start = origin - context_seconds
    elif advertisements:
        origin = advertisements[0].timestamp
        setup_start = origin - context_seconds
    else:
        origin = records[0].timestamp if records else 0.0
        setup_start = origin

    _write_line(output, f"# source: {path}")
    _write_line(output, f"# models: {', '.join(models)}")
    _write_line(
        output,
        "# addresses: "
        + (", ".join(sorted(discovered_addresses | addresses)) or "none"),
    )
    _write_line(
        output,
        "# ACL handles: "
        + (", ".join(f"0x{handle:03x}" for handle in sorted(handles)) or "none"),
    )
    _write_line(
        output, f"# setup context: {context_seconds:.1f}s before first target ACL"
    )
    _write_line(output)

    timeline: list[tuple[float, int, str]] = []
    for advertisement in advertisements:
        if not target_sdus or setup_start <= advertisement.timestamp <= origin:
            timeline.append(
                (
                    advertisement.timestamp,
                    advertisement.record_index,
                    "ADV "
                    f"address={advertisement.address} rssi={advertisement.rssi} "
                    f"name={advertisement.name!r} data={advertisement.data.hex(' ')}",
                )
            )

    target_address_bytes = tuple(
        _address_on_wire(address) for address in discovered_addresses | addresses
    )
    setup_commands = {
        record.index
        for record in records
        if setup_start <= record.timestamp <= origin
        and (opcode := _hci_command_opcode(record)) is not None
        and (
            opcode in CONNECTION_OPCODES
            or any(address in record.data for address in target_address_bytes)
        )
    }
    setup_opcodes = {
        opcode
        for record in records
        if record.index in setup_commands
        and (opcode := _hci_command_opcode(record)) is not None
    }

    for record in records:
        result_opcode = _hci_result_opcode(record)
        is_setup_context = record.index in setup_commands or (
            setup_start <= record.timestamp <= origin and result_opcode in setup_opcodes
        )
        if is_setup_context or _target_link_record(record, handles):
            timeline.append(
                (
                    record.timestamp,
                    record.index,
                    _hci_description(record),
                )
            )

    for sdu in target_sdus:
        if sdu.cid == ATT_CID:
            description = _att_description(sdu.payload)
            channel = "ATT"
        elif sdu.cid == LE_SIGNALING_CID:
            description = "LE signaling"
            channel = "L2CAP"
        else:
            description = "target connection data"
            channel = "L2CAP"
        records_text = ",".join(str(index) for index in sdu.record_indices)
        timeline.append(
            (
                sdu.timestamp,
                sdu.record_indices[0],
                f"{channel}_{sdu.direction} handle=0x{sdu.handle:03x} "
                f"cid=0x{sdu.cid:04x} records={records_text} {description} "
                f"payload={sdu.payload.hex(' ')}",
            )
        )

    seen: set[tuple[int, str]] = set()
    for timestamp, record_index, description in sorted(timeline):
        identity = (record_index, description)
        if identity in seen:
            continue
        seen.add(identity)
        _write_line(
            output,
            f"{_format_timestamp(timestamp, origin)} "
            f"record={record_index:05d} {description}",
        )

    _write_line(output)
    _write_line(
        output,
        f"# extracted {len(seen)} timeline entries from "
        f"{len(records)} PacketLogger records",
    )


def _input_paths(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for input_path in inputs:
        if input_path.is_dir():
            paths.extend(sorted(input_path.glob("*.pklg")))
        elif input_path.is_file():
            paths.append(input_path)
        else:
            raise FileNotFoundError(input_path)
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help=".pklg file or directory")
    parser.add_argument(
        "--model",
        choices=("auto", "h7124", "h7129"),
        default="auto",
        help="target model; auto infers it from each filename",
    )
    parser.add_argument(
        "--address",
        action="append",
        default=[],
        help="restrict extraction to this purifier address; may be repeated",
    )
    parser.add_argument(
        "--context-seconds",
        type=float,
        default=3.0,
        help="controller setup context retained before the first target ACL packet",
    )
    parser.add_argument("--output", type=Path, help="write output to this file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        paths = _input_paths(args.inputs)
        addresses = {_normalize_address(address) for address in args.address}
        if args.output:
            with args.output.open("wb") as output:
                for index, path in enumerate(paths):
                    if index:
                        _write_line(output, "\n" + "=" * 100 + "\n")
                    extract_trace(
                        path,
                        output,
                        requested_model=args.model,
                        addresses=addresses,
                        context_seconds=args.context_seconds,
                    )
        else:
            output = sys.stdout.buffer
            for index, path in enumerate(paths):
                if index:
                    _write_line(output, "\n" + "=" * 100 + "\n")
                extract_trace(
                    path,
                    output,
                    requested_model=args.model,
                    addresses=addresses,
                    context_seconds=args.context_seconds,
                )
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
