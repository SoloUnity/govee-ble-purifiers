"""Deterministic tests for integration module boundaries and stable facades."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from custom_components.govee_ble_air_purifier import bluetooth, profiles, protocol
from custom_components.govee_ble_air_purifier import client as client_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    cleanup,
    environment,
    transport,
)
from custom_components.govee_ble_air_purifier.bluetooth import (
    errors as bluetooth_errors,
)
from custom_components.govee_ble_air_purifier.operations import (
    CommandDeadlineExceeded,
    CommandSuperseded,
    PurifierClientError,
    _Operation,
)
from custom_components.govee_ble_air_purifier.profiles import (
    errors as profile_errors,
)
from custom_components.govee_ble_air_purifier.protocol.types import ProtocolError
from custom_components.govee_ble_air_purifier.transactions import (
    RefreshPreemptedError,
    TransactionTimeoutError,
)

PACKAGE = Path(__file__).parents[1] / "custom_components" / "govee_ble_air_purifier"
PACKAGE_IMPORT = "custom_components.govee_ble_air_purifier"

ENTITY_MODULES = {"entity", "fan", "light", "sensor"}
HOME_ASSISTANT_OR_BLUETOOTH_IMPORTS = {
    "bleak",
    "bleak_retry_connector",
    "bluetooth",
    "homeassistant",
}
UPPER_LAYER_MODULES = {"config_flow", "coordinator", *ENTITY_MODULES}


def _package_files(directory: str) -> tuple[Path, ...]:
    """Return a stable list of Python modules in one package directory."""
    return tuple(sorted((PACKAGE / directory).rglob("*.py")))


def _import_root(module: str) -> str:
    """Return a dependency root for an absolute import."""
    if module == PACKAGE_IMPORT:
        return ""
    package_prefix = f"{PACKAGE_IMPORT}."
    if module.startswith(package_prefix):
        return module.removeprefix(package_prefix).split(".", maxsplit=1)[0]
    return module.split(".", maxsplit=1)[0]


def _imports(path: Path) -> tuple[tuple[int, str], ...]:
    """Return normalized import roots with source lines from one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(
                (node.lineno, _import_root(alias.name)) for alias in node.names
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            if node.module:
                imported.append((node.lineno, node.module.split(".", maxsplit=1)[0]))
            else:
                imported.extend(
                    (node.lineno, alias.name.split(".", maxsplit=1)[0])
                    for alias in node.names
                )
            continue
        if node.module == PACKAGE_IMPORT:
            imported.extend(
                (node.lineno, alias.name.split(".", maxsplit=1)[0])
                for alias in node.names
            )
        elif node.module:
            imported.append((node.lineno, _import_root(node.module)))
    return tuple(imported)


BLUETOOTH_FORBIDDEN = {
    "bluetooth_profile",
    "channel",
    "client",
    "config_flow",
    "coordinator",
    "discovery",
    "models",
    "operations",
    "profiles",
    "protocol",
    "recovery",
    "setup_validation",
    "state_reducer",
    "transactions",
    *ENTITY_MODULES,
}
PROTOCOL_FORBIDDEN = {
    "bluetooth_profile",
    "channel",
    "client",
    "config_flow",
    "coordinator",
    "discovery",
    "operations",
    "recovery",
    "setup_validation",
    "state_reducer",
    "transactions",
    *ENTITY_MODULES,
    *HOME_ASSISTANT_OR_BLUETOOTH_IMPORTS,
}
PROFILES_FORBIDDEN = {
    "bluetooth_profile",
    "channel",
    "client",
    "config_flow",
    "coordinator",
    "discovery",
    "operations",
    "protocol",
    "recovery",
    "setup_validation",
    "state_reducer",
    "transactions",
    *ENTITY_MODULES,
    *HOME_ASSISTANT_OR_BLUETOOTH_IMPORTS,
}
SYNC_POLICY_FORBIDDEN = {
    "asyncio",
    "bluetooth_profile",
    "client",
    "config_flow",
    "coordinator",
    "discovery",
    "setup_validation",
    *ENTITY_MODULES,
    *HOME_ASSISTANT_OR_BLUETOOTH_IMPORTS,
}
LOWER_LAYER_FILES = (
    PACKAGE / "bluetooth_profile.py",
    PACKAGE / "channel.py",
    PACKAGE / "client.py",
    PACKAGE / "crypto.py",
    PACKAGE / "frame.py",
    PACKAGE / "models.py",
    PACKAGE / "operations.py",
    PACKAGE / "recovery.py",
    PACKAGE / "state_reducer.py",
    PACKAGE / "transactions.py",
    *_package_files("bluetooth"),
    *_package_files("profiles"),
    *_package_files("protocol"),
)


@pytest.mark.parametrize(
    ("paths", "forbidden"),
    [
        pytest.param((PACKAGE / "models.py",), {"profiles"}, id="models"),
        pytest.param(
            _package_files("bluetooth"),
            BLUETOOTH_FORBIDDEN,
            id="bluetooth-package",
        ),
        pytest.param(
            _package_files("protocol"),
            PROTOCOL_FORBIDDEN,
            id="protocol-package",
        ),
        pytest.param(
            _package_files("profiles"),
            PROFILES_FORBIDDEN,
            id="profiles-package",
        ),
        pytest.param(
            (PACKAGE / "state_reducer.py", PACKAGE / "recovery.py"),
            SYNC_POLICY_FORBIDDEN,
            id="synchronous-policy",
        ),
        pytest.param(
            LOWER_LAYER_FILES,
            UPPER_LAYER_MODULES,
            id="no-upward-ui-or-coordinator-imports",
        ),
    ],
)
def test_module_boundaries(paths: tuple[Path, ...], forbidden: set[str]) -> None:
    """Lower layers cannot acquire imports owned by an upper layer."""
    violations = [
        f"{path.relative_to(PACKAGE)}:{line} imports {root}"
        for path in paths
        for line, root in _imports(path)
        if root in forbidden
    ]

    assert violations == []


def test_compatibility_facades_preserve_canonical_exception_identities() -> None:
    """Package extraction must not split exception-catching identities."""
    assert bluetooth.BluetoothUnavailableError is (
        bluetooth_errors.BluetoothUnavailableError
    )
    assert bluetooth.GattTransportError is bluetooth_errors.GattTransportError
    assert profiles.ProfileError is profile_errors.ProfileError
    assert (
        profiles.DuplicateProfileKeyError
        is profile_errors.DuplicateProfileKeyError
    )
    assert profiles.ProfileSelectionError is profile_errors.ProfileSelectionError
    assert protocol.ProtocolError is ProtocolError
    assert client_module.PurifierClientError is PurifierClientError
    assert client_module.CommandDeadlineExceeded is CommandDeadlineExceeded
    assert client_module.CommandSuperseded is CommandSuperseded
    assert client_module.TransactionTimeoutError is TransactionTimeoutError
    assert client_module._Operation is _Operation
    assert client_module._RefreshPreempted is RefreshPreemptedError


def test_extracted_bluetooth_modules_keep_the_legacy_logger_namespace() -> None:
    """Existing Bluetooth log filters continue to match extracted owners."""
    expected_namespace = bluetooth.__name__

    assert cleanup._LOGGER.name == expected_namespace
    assert environment._LOGGER.name == expected_namespace
    assert transport._LOGGER.name == expected_namespace
