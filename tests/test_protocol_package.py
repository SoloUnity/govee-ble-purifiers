"""Tests for the protocol package facade and internal ownership boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from custom_components.govee_ble_air_purifier import protocol
from custom_components.govee_ble_air_purifier.protocol.matcher import (
    ResponseMatcher,
)
from custom_components.govee_ble_air_purifier.protocol.service import (
    GoveePurifierProtocol,
)
from custom_components.govee_ble_air_purifier.protocol.types import (
    MatchResult,
    ProtocolError,
    RequestDescriptor,
    ResponseKind,
    ResponseSpec,
)


def test_facade_reexports_canonical_protocol_identities() -> None:
    """Facade imports and internal imports resolve to the same runtime objects."""

    assert protocol.GoveePurifierProtocol is GoveePurifierProtocol
    assert protocol.RequestDescriptor is RequestDescriptor
    assert protocol.ResponseMatcher is ResponseMatcher
    assert protocol.MatchResult is MatchResult
    assert protocol.ProtocolError is ProtocolError
    assert protocol.ResponseKind is ResponseKind
    assert protocol.ResponseSpec is ResponseSpec


def test_facade_declares_its_stable_public_surface() -> None:
    """Wildcard imports stay bounded to the deliberately supported facade."""

    assert protocol.__all__ == (
        "GoveePurifierProtocol",
        "MatchResult",
        "ProtocolError",
        "RequestDescriptor",
        "ResponseKind",
        "ResponseMatcher",
        "ResponseSpec",
        "initialization_requests",
        "refresh_requests",
    )


def test_protocol_internals_do_not_depend_on_transport_or_runtime_layers() -> None:
    """The pure protocol package cannot grow an upward runtime dependency."""

    forbidden = {
        "bluetooth",
        "client",
        "config_flow",
        "coordinator",
        "discovery",
        "entity",
        "recovery",
        "state_reducer",
    }
    protocol_directory = (
        Path(__file__).parents[1]
        / "custom_components"
        / "govee_ble_air_purifier"
        / "protocol"
    )

    for path in protocol_directory.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_modules.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imported_modules.isdisjoint(forbidden), path.name
