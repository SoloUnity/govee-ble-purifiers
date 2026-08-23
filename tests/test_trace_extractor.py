"""Tests for the Apple PacketLogger purifier trace extractor."""

from __future__ import annotations

import struct
from pathlib import Path

from scripts.extract_air_purifier_trace import (
    ACL_RX,
    ATT_CID,
    HCI_EVENT,
    PacketRecord,
    _connection_addresses,
    _target_handles,
    extended_advertisements,
    read_packetlogger,
    reassemble_l2cap,
)


def _pklg_record(packet_type: int, data: bytes, *, microseconds: int = 0) -> bytes:
    length = 9 + len(data)
    return struct.pack("<IIIB", length, 1_700_000_000, microseconds, packet_type) + data


def _extended_advertisement(name: str, address: bytes, rssi: int) -> bytes:
    advertising_data = b"\x02\x01\x05" + bytes((len(name) + 1, 0x09)) + name.encode()
    report = (
        b"\x13\x00"
        + b"\x00"
        + address
        + b"\x01\x00\x00\x7f"
        + struct.pack("b", rssi)
        + b"\x00\x00\x00"
        + b"\x00" * 6
        + bytes((len(advertising_data),))
        + advertising_data
    )
    parameters = b"\x0d\x01" + report
    return bytes((0x3E, len(parameters))) + parameters


def test_reads_packetlogger_and_decodes_target_advertisement(tmp_path: Path) -> None:
    """A binary PacketLogger record yields its H7129 name, address, and RSSI."""
    event = _extended_advertisement(
        "ihoment_H7129_6A7D",
        bytes.fromhex("7d 6a f9 53 e7 5c"),
        -69,
    )
    trace = tmp_path / "trace.pklg"
    trace.write_bytes(_pklg_record(HCI_EVENT, event, microseconds=123_000))

    records = read_packetlogger(trace)
    advertisements = extended_advertisements(records[0])

    assert len(advertisements) == 1
    assert advertisements[0].address == "5C:E7:53:F9:6A:7D"
    assert advertisements[0].name == "ihoment_H7129_6A7D"
    assert advertisements[0].rssi == -69


def test_reassembles_fragmented_target_att_notification() -> None:
    """Continuation ACL packets remain attached to the purifier connection."""
    att_payload = b"\x1b\x16\x00" + bytes(range(20))
    l2cap = struct.pack("<HH", len(att_payload), ATT_CID) + att_payload
    first = l2cap[:11]
    continuation = l2cap[11:]
    records = [
        PacketRecord(
            10,
            1.0,
            ACL_RX,
            struct.pack("<HH", 0x2000 | 0x0041, len(first)) + first,
        ),
        PacketRecord(
            11,
            1.001,
            ACL_RX,
            struct.pack("<HH", 0x1000 | 0x0041, len(continuation)) + continuation,
        ),
    ]

    sdus = reassemble_l2cap(records)

    assert len(sdus) == 1
    assert sdus[0].record_indices == (10, 11)
    assert sdus[0].payload == att_payload
    assert _target_handles(sdus, ("h7129",)) == {0x0041}


def test_maps_enhanced_connection_complete_to_peer_address() -> None:
    """The H7129 address is bound to its connection handle for later ACL filtering."""
    event = bytes.fromhex(
        "3e 22 29 00 41 00 00 00 7d 6a f9 53 e7 5c "
        "00 00 00 00 00 00 00 00 00 00 00 00 18 00 00 00 48 00 00 ff ff ff"
    )
    records = [PacketRecord(237, 1.0, HCI_EVENT, event)]

    assert _connection_addresses(records) == {0x0041: "5C:E7:53:F9:6A:7D"}
