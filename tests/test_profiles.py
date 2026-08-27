"""Tests for strict bundled, immutable purifier model profiles."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    GattTransport,
    HomeAssistantBluetoothEnvironment,
)
from custom_components.govee_ble_air_purifier.client import ReliablePurifierClient
from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import FanMode, SetFanMode
from custom_components.govee_ble_air_purifier.profiles import (
    DuplicateProfileKeyError,
    Model,
    ProfileError,
    SecurityMode,
    get_profile_registry,
    load_profile_registry,
)
from custom_components.govee_ble_air_purifier.protocol import GoveePurifierProtocol
from scripts.validate_model_profiles import main as validate_profiles_main

PROFILE_DIR = (
    Path(__file__).parents[1]
    / "custom_components"
    / "govee_ble_air_purifier"
    / "model_profiles"
)


def _copy_profiles(tmp_path: Path) -> Path:
    destination = tmp_path / "model_profiles"
    shutil.copytree(PROFILE_DIR, destination)
    return destination


def _edit_profile(
    directory: Path,
    profile_id: str,
    edit: Callable[[dict[str, object]], None],
) -> None:
    path = directory / f"{profile_id}.json"
    raw = json.loads(path.read_text())
    edit(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_bundled_profiles_resolve_expected_lineage_and_identity() -> None:
    registry = load_profile_registry()

    h7124 = registry.for_model(Model.H7124)
    h7129 = registry.for_model(Model.H7129)
    assert h7124.lineage == ("h7124", "default")
    assert h7129.lineage == ("h7129", "default-encrypted")
    assert h7124.channel.strategy is SecurityMode.PLAINTEXT
    assert h7124.channel.negotiation is None
    assert h7129.channel.strategy is SecurityMode.H7129_SESSION
    assert h7129.channel.negotiation is not None
    assert len(h7124.fingerprint) == 64
    assert len(h7129.fingerprint) == 64


def test_registry_is_cached_and_values_are_immutable() -> None:
    first = get_profile_registry()
    second = get_profile_registry()
    profile = first.for_model(Model.H7124)

    assert first is second
    assert first.for_model(Model.H7124) is second.for_model(Model.H7124)
    with pytest.raises(FrozenInstanceError):
        profile.profile_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        profile.protocol.request_catalog["changed"] = object()  # type: ignore[index]


def test_runtime_layers_share_the_exact_resolved_profile_instance() -> None:
    profile = load_profile_registry().for_model(Model.H7129)
    environment = HomeAssistantBluetoothEnvironment(
        object(),  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
        profile,
    )
    transport = GattTransport(name="purifier", profile=profile)
    protocol = GoveePurifierProtocol(profile)
    client = ReliablePurifierClient(
        environment=environment,
        transport=transport,
        protocol=protocol,
        profile=profile,
        state_callback=lambda _state: None,
        availability_callback=lambda _available, _error: None,
    )

    assert environment.profile is profile
    assert transport.profile is profile
    assert protocol.profile is profile
    assert client._profile is profile


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("GVH7124", Model.H7124),
        ("gvh7124_bedroom", Model.H7124),
        ("ihoment_H7129_6A7D", Model.H7129),
        ("IHOMENT_h7129_TEST", Model.H7129),
    ],
)
def test_name_selection_matches_only_exact_profiles(name: str, model: Model) -> None:
    match = load_profile_registry().match_name(name)
    assert match is not None
    assert match.model is model


@pytest.mark.parametrize(
    "name",
    [None, "", "GVH712", "H7124", "ihoment_H7129", "GVH7125_TEST"],
)
def test_unknown_near_miss_and_nameless_traffic_stay_unsupported(
    name: str | None,
) -> None:
    assert load_profile_registry().match_name(name) is None


def test_roots_are_complete_but_not_discoverable() -> None:
    registry = load_profile_registry()
    for profile_id in ("default", "default-encrypted"):
        root = registry.profiles[profile_id]
        assert root.identity.model is None
        assert root.identity.advertised_name_prefixes == ()
        assert root.protocol.request_catalog


def test_arrays_replace_whole_parent_values(tmp_path: Path) -> None:
    directory = _copy_profiles(tmp_path)

    def edit(raw: dict[str, object]) -> None:
        raw["identity"] = {
            "advertised_name_prefixes": ["CUSTOM_H7124_"],
            "manufacturer": "Govee",
            "model": "H7124",
            "display_name": "Govee H7124",
            "support_status": "verified",
        }

    _edit_profile(directory, "h7124", edit)
    registry = load_profile_registry(directory)
    assert registry.for_model(Model.H7124).identity.advertised_name_prefixes == (
        "CUSTOM_H7124_",
    )


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    directory = _copy_profiles(tmp_path)
    path = directory / "h7124.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"profile_id":"h7124",'
        '"extends":"default"}',
        encoding="utf-8",
    )
    with pytest.raises(DuplicateProfileKeyError, match="duplicate JSON key"):
        load_profile_registry(directory)


@pytest.mark.parametrize(
    ("profile_id", "edit", "message"),
    [
        ("h7129", lambda raw: raw.update(extends="../default"), "must extend"),
        ("h7129", lambda raw: raw.update(schema_version=2), "schema_version"),
        ("h7129", lambda raw: raw.update(schema_version=True), "schema_version"),
        (
            "h7129",
            lambda raw: raw.update(session_key="do-not-load"),
            "forbidden",
        ),
        (
            "default",
            lambda raw: raw["bluetooth"].update(service_uuid="invalid"),
            "valid UUID",
        ),
        (
            "default",
            lambda raw: raw["timings"].update(connect_attempts=4),
            "between 1 and 3",
        ),
        (
            "default",
            lambda raw: raw["protocol"]["request_catalog"][0].update(frame="33 b2"),
            "valid application frame",
        ),
        (
            "default",
            lambda raw: raw["protocol"]["commands"]["power"].update(
                prefix="33 02"
            ),
            "unsafe command template",
        ),
        (
            "default",
            lambda raw: raw["protocol"].update(
                periodic_request="not_in_catalog"
            ),
            "unresolved",
        ),
        (
            "default",
            lambda raw: raw["capabilities"].update(pm25="yes"),
            "capability values",
        ),
        (
            "default",
            lambda raw: raw["custom_auto_defaults"].update(
                pm25_boundaries=[3, 5, 5, 15]
            ),
            "strictly ascending",
        ),
        (
            "h7124",
            lambda raw: raw.update(unknown_field=True),
            "unknown fields",
        ),
        (
            "default",
            lambda raw: raw.update(identity=[]),
            "identity must be an object",
        ),
    ],
)
def test_invalid_artifacts_fail_closed_without_default_fallback(
    tmp_path: Path,
    profile_id: str,
    edit: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    directory = _copy_profiles(tmp_path)
    _edit_profile(directory, profile_id, edit)
    with pytest.raises(ProfileError, match=message):
        load_profile_registry(directory)


def test_ambiguous_exact_name_prefixes_are_rejected(tmp_path: Path) -> None:
    directory = _copy_profiles(tmp_path)

    def edit(raw: dict[str, object]) -> None:
        raw["identity"]["advertised_name_prefixes"] = ["GVH7124_TEST"]

    _edit_profile(directory, "h7129", edit)
    with pytest.raises(ProfileError, match="ambiguous advertised-name prefixes"):
        load_profile_registry(directory)


def test_schema_artifact_is_loaded_atomically_with_profiles(tmp_path: Path) -> None:
    directory = _copy_profiles(tmp_path)
    _edit_profile(
        directory,
        "schema",
        lambda raw: raw.update({"$schema": "https://example.invalid/schema"}),
    )
    with pytest.raises(ProfileError, match="draft 2020-12"):
        load_profile_registry(directory)


@pytest.mark.parametrize(
    ("model", "expected_auto", "expected_initial_count"),
    [(Model.H7124, 0x14, 23), (Model.H7129, 0x12, 24)],
)
def test_protocol_vectors_and_request_order_are_regression_equivalent(
    model: Model,
    expected_auto: int,
    expected_initial_count: int,
) -> None:
    profile = load_profile_registry().for_model(model)
    protocol = GoveePurifierProtocol(profile)

    assert len(protocol.initialization_requests()) == expected_initial_count
    assert [request.name for request in protocol.refresh_requests()] == [
        "capability_b5",
        "device_state",
        "mode_data_00",
        "mode_data_01",
        "mode_data_03",
        "night_light_state",
        "night_light_color",
        "capability_1e_01_02",
        "capability_10",
        "capability_08",
        "capability_26",
        "structured_16",
        "capability_17",
        "air_quality",
    ]
    assert protocol.device_state_poll().frame == build_frame(b"\xaa\x01")
    assert profile.protocol.periodic_request == profile.protocol.essential_request
    assert protocol.encode(SetFanMode(FanMode.AUTO)) == build_frame(
        bytes((0x3A, 0x05, 0x03, 0, 0, expected_auto))
    )


def test_custom_auto_defaults_are_inactive_profile_data() -> None:
    registry = load_profile_registry()
    h7124 = registry.for_model(Model.H7124).custom_auto_defaults
    h7129 = registry.for_model(Model.H7129).custom_auto_defaults

    assert h7124.pm25_boundaries == (3, 5, 9, 15)
    assert h7129.pm25_boundaries == (7, 9, 13, 19)
    assert (
        h7124.modes
        == h7129.modes
        == (
            FanMode.SLEEP,
            FanMode.LOW,
            FanMode.MEDIUM,
            FanMode.HIGH,
            FanMode.TURBO,
        )
    )
    assert h7124.upshift_confirmation_seconds == 3
    assert h7124.downshift_delays_minutes == (7, 5, 5, 5)


def test_profile_diagnostics_are_safe_and_bounded() -> None:
    diagnostics = (
        load_profile_registry()
        .for_model(Model.H7129)
        .diagnostic_snapshot(requested_model="H7129")
    )
    serialized = json.dumps(diagnostics)

    assert diagnostics["profile_id"] == "h7129"
    assert diagnostics["lineage"] == ("h7129", "default-encrypted")
    assert diagnostics["security_strategy"] == "h7129_session"
    assert diagnostics["request_count"] == 24
    assert "address" not in serialized.casefold()
    assert "session_key" not in serialized.casefold()


def test_profile_artifacts_are_packaged_and_standalone_validator_passes() -> None:
    assert {path.name for path in PROFILE_DIR.glob("*.json")} == {
        "schema.json",
        "default.json",
        "default-encrypted.json",
        "h7124.json",
        "h7129.json",
    }
    assert validate_profiles_main() == 0


def test_schema_closes_every_declared_object() -> None:
    schema = json.loads((PROFILE_DIR / "schema.json").read_text())

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(schema)
