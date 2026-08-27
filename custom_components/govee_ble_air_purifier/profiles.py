"""Strict bundled model-profile loading and immutable runtime values."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast
from uuid import UUID

from .frame import validate_frame

SCHEMA_VERSION: Final = 1
PROFILE_FILENAMES: Final[tuple[str, ...]] = (
    "default.json",
    "default-encrypted.json",
    "h7124.json",
    "h7129.json",
)
ROOT_PROFILE_IDS: Final[frozenset[str]] = frozenset({"default", "default-encrypted"})
EXACT_PROFILE_PARENTS: Final[Mapping[str, str]] = MappingProxyType(
    {"h7124": "default", "h7129": "default-encrypted"}
)

# These are reviewed safety ceilings, not device behavior defaults. Effective
# values remain owned by the bundled profile artifacts below these bounds.
MAX_ATTEMPTS: Final = 3
MAX_GATT_TIMEOUT: Final = 45.0
MAX_REQUEST_TIMEOUT: Final = 15.0
MAX_COMMAND_DEADLINE: Final = 120.0
MAX_STARTUP_TIMEOUT: Final = 300.0
MAX_BACKOFF: Final = 60.0
MAX_RECOVERY_WINDOW: Final = 300.0

_PROFILE_DIR = Path(__file__).with_name("model_profiles")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REQUEST_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})(?: [0-9a-f]{2})*$")
_SECRET_FIELDS = frozenset(
    {
        "callback",
        "communication_key",
        "decrypted_frame",
        "encrypted_frame",
        "import",
        "import_path",
        "key",
        "negotiation_randomness",
        "password",
        "random_padding",
        "secret",
        "session_key",
        "user_path",
    }
)


class ProfileError(ValueError):
    """Base error for an invalid bundled profile artifact."""


class DuplicateProfileKeyError(ProfileError):
    """Raised when JSON contains a duplicate object key."""


class ProfileSelectionError(ProfileError):
    """Raised when an exact model or advertised name cannot be selected safely."""


class Model(StrEnum):
    """Supported purifier model values retained in config entries."""

    H7124 = "H7124"
    H7129 = "H7129"


class SecurityMode(StrEnum):
    """Closed application-channel strategy identifiers."""

    PLAINTEXT = "plaintext"
    H7129_SESSION = "h7129_session"


class FanMode(StrEnum):
    """Fan modes whose wire representation is documented."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SLEEP = "sleep"
    AUTO = "auto"
    TURBO = "turbo"


class SupportStatus(StrEnum):
    """Closed support-state values exposed in diagnostics."""

    BASELINE = "baseline"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    manufacturer: str
    model: Model | None
    display_name: str
    support_status: SupportStatus
    advertised_name_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BluetoothProfile:
    service_uuid: str
    notify_uuid: str
    write_uuid: str


@dataclass(frozen=True, slots=True)
class NegotiationPolicy:
    attempts: int
    retry_interval: float
    phase_timeout: float
    step_delay: float


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    strategy: SecurityMode
    first_application_delay: float
    negotiation: NegotiationPolicy | None


@dataclass(frozen=True, slots=True)
class MatcherDefinition:
    kind: str
    prefix: bytes = b""
    selector: bytes = b""
    exact: bytes = b""
    exact_alternatives: tuple[bytes, ...] = ()
    fragments: tuple[int, ...] = ()
    allowed_prefixes: tuple[bytes, ...] = ()
    expected_fields: tuple[tuple[int, int], ...] = ()
    allowed_values: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestDefinition:
    name: str
    frame: bytes
    response: MatcherDefinition


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    strategy: str
    prefix: bytes


@dataclass(frozen=True, slots=True)
class ProtocolProfile:
    codec: str
    checksum: str
    frame_size: int
    auto_parameter: int
    startup_mode_strategy: str
    commands: Mapping[str, CommandDefinition]
    request_catalog: Mapping[str, RequestDefinition]
    initialization_order: tuple[str, ...]
    refresh_order: tuple[str, ...]
    essential_request: str
    periodic_request: str


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    power: bool
    fan: bool
    light: bool
    pm25: bool
    filter_life: bool
    unsolicited_updates: bool
    refresh: bool

    def as_dict(self) -> dict[str, bool]:
        """Return safe primitive capability diagnostics."""
        return {
            "power": self.power,
            "fan": self.fan,
            "light": self.light,
            "pm25": self.pm25,
            "filter_life": self.filter_life,
            "unsolicited_updates": self.unsolicited_updates,
            "refresh": self.refresh,
        }


@dataclass(frozen=True, slots=True)
class TimingProfile:
    connect_attempts: int
    connection_attempt_timeout: float
    connection_abort_timeout: float
    connection_diagnostic_timeout: float
    notification_subscribe_timeout: float
    gatt_write_timeout: float
    gatt_disconnect_timeout: float
    gatt_operation_cancel_timeout: float
    stale_connection_cleanup_timeout: float
    stale_connection_check_interval: float
    advertisement_check_interval: float
    recent_cached_advertisement_max_age: float
    fresh_advertisement_timeout: float
    transaction_timeout: float
    initialization_attempts: int
    essential_initialization_max_batches: int
    initialization_retry_delay: float
    periodic_poll_attempts: int
    refresh_attempts: int
    command_send_attempts: int
    command_deadline: float
    startup_timeout: float
    between_request_delay: float
    poll_interval: float
    initial_poll_delay: float
    backoff_initial: float
    backoff_max: float
    backoff_reset_after: float
    recent_advertisement_backoff_max: float
    advertisement_settle_delay: float
    recovery_storm_window: float
    recovery_storm_failure_threshold: int
    recovery_storm_advertisement_threshold: int
    recovery_storm_initial_floor: float
    recovery_storm_max_floor: float

    def as_dict(self) -> dict[str, int | float]:
        """Return safe effective timing diagnostics."""
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class CustomAutoDefaults:
    modes: tuple[FanMode, ...]
    pm25_boundaries: tuple[int, ...]
    upshift_confirmation_seconds: int
    downshift_delays_minutes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """One complete, immutable, validated effective model profile."""

    schema_version: int
    profile_id: str
    lineage: tuple[str, ...]
    source_basename: str
    fingerprint: str
    identity: IdentityProfile
    bluetooth: BluetoothProfile
    channel: ChannelProfile
    protocol: ProtocolProfile
    capabilities: CapabilityProfile
    timings: TimingProfile
    custom_auto_defaults: CustomAutoDefaults

    @property
    def model(self) -> Model:
        """Return the exact supported model; roots cannot enter runtime."""
        if self.identity.model is None:
            raise ProfileSelectionError(
                f"baseline profile {self.profile_id!r} has no supported model"
            )
        return self.identity.model

    @property
    def security(self) -> SecurityMode:
        """Compatibility property for channel selection."""
        return self.channel.strategy

    @property
    def auto_parameter(self) -> int:
        """Compatibility property for protocol command assembly."""
        return self.protocol.auto_parameter

    def diagnostic_snapshot(
        self, *, requested_model: str | None = None
    ) -> dict[str, Any]:
        """Return safe profile metadata without device identity or raw JSON."""
        return {
            "requested_model": requested_model,
            "profile_id": self.profile_id,
            "lineage": self.lineage,
            "schema_version": self.schema_version,
            "support_status": self.identity.support_status.value,
            "security_strategy": self.channel.strategy.value,
            "source_basename": self.source_basename,
            "fingerprint": self.fingerprint,
            "capabilities": self.capabilities.as_dict(),
            "request_names": tuple(self.protocol.request_catalog),
            "request_count": len(self.protocol.request_catalog),
            "initialization_request_count": len(self.protocol.initialization_order),
            "refresh_request_count": len(self.protocol.refresh_order),
            "timings": self.timings.as_dict(),
            "service_uuid": self.bluetooth.service_uuid,
            "notify_uuid": self.bluetooth.notify_uuid,
            "write_uuid": self.bluetooth.write_uuid,
        }

    @classmethod
    def for_model(cls, model: Model | str) -> DeviceProfile:
        """Compatibility loader backed by the immutable bundled registry."""
        return get_profile_registry().for_model(model)


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    """One atomically loaded set of bundled profiles."""

    profiles: Mapping[str, DeviceProfile]

    def for_model(self, model: Model | str) -> DeviceProfile:
        """Resolve an existing config-entry model to its exact profile."""
        selected = {
            Model.H7124: "h7124",
            Model.H7129: "h7129",
        }[Model(model)]
        try:
            return self.profiles[selected]
        except KeyError as err:
            raise ProfileSelectionError(
                f"exact bundled profile {selected!r} is unavailable"
            ) from err

    def match_name(self, name: str | None) -> DeviceProfile | None:
        """Match only exact profiles' explicit case-insensitive prefixes."""
        if not name:
            return None
        normalized = name.casefold()
        matches = [
            profile
            for profile_id, profile in self.profiles.items()
            if profile_id in EXACT_PROFILE_PARENTS
            and any(
                normalized.startswith(prefix.casefold())
                for prefix in profile.identity.advertised_name_prefixes
            )
        ]
        if len(matches) > 1:
            raise ProfileSelectionError(
                f"advertised name matches multiple exact profiles: {name!r}"
            )
        return matches[0] if matches else None


_registry_lock = threading.Lock()
_registry: ProfileRegistry | None = None


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateProfileKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_object
        )
    except (OSError, json.JSONDecodeError) as err:
        raise ProfileError(
            f"unable to decode bundled profile {path.name}: {err}"
        ) from err
    if not isinstance(decoded, dict):
        raise ProfileError(f"bundled profile {path.name} must be a JSON object")
    return decoded


def _validate_schema_artifact(schema: Mapping[str, Any]) -> None:
    """Reject a missing or unexpectedly shaped bundled schema contract."""
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ProfileError("schema.json must declare JSON Schema draft 2020-12")
    if not isinstance(schema.get("$defs"), dict) or not isinstance(
        schema.get("oneOf"), list
    ):
        raise ProfileError("schema.json is missing its profile definitions")

    def validate_closed_objects(value: Any) -> None:
        if isinstance(value, dict):
            if (
                value.get("type") == "object"
                and value.get("additionalProperties") is not False
            ):
                raise ProfileError(
                    "schema.json must set additionalProperties false on every object"
                )
            for child in value.values():
                validate_closed_objects(child)
        elif isinstance(value, list):
            for child in value:
                validate_closed_objects(child)

    validate_closed_objects(schema)


def _object(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must be an object")
    return value


def _array(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProfileError(f"{path} must be an array")
    return value


def _reject_secret_fields(value: Any, *, path: str = "$.") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _SECRET_FIELDS:
                raise ProfileError(f"forbidden secret/executable field at {path}{key}")
            _reject_secret_fields(child, path=f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, path=f"{path}{index}.")


def _require_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    path: str,
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise ProfileError(f"{path} missing required fields: {sorted(missing)}")
    if unknown:
        raise ProfileError(f"{path} has unknown fields: {sorted(unknown)}")


def _deep_merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(parent)
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(cast(dict[str, Any], merged[key]), value)
        else:
            merged[key] = value
    return merged


def _hex_bytes(value: Any, *, path: str, frame: bool = False) -> bytes:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise ProfileError(f"{path} must be lowercase space-separated hex bytes")
    data = bytes.fromhex(value)
    if frame:
        try:
            return validate_frame(data)
        except ValueError as err:
            raise ProfileError(
                f"{path} is not a valid application frame: {err}"
            ) from err
    return data


def _number(
    value: Any,
    *,
    path: str,
    minimum: float = 0,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProfileError(f"{path} must be numeric")
    if integer and not isinstance(value, int):
        raise ProfileError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ProfileError(f"{path} must be between {minimum} and {maximum}")
    return value


def _string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{path} must be a non-empty string")
    return value


def _string_tuple(value: Any, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProfileError(f"{path} must be an array of strings")
    return tuple(value)


_MATCHER_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "exact": frozenset({"kind", "exact", "exact_alternatives"}),
        "value_byte": frozenset(
            {"kind", "prefix", "allowed_values", "exact_alternatives"}
        ),
        "zero_payload": frozenset({"kind", "prefix", "exact_alternatives"}),
        "prefix": frozenset({"kind", "prefix", "exact_alternatives"}),
        "prefix_selector": frozenset(
            {"kind", "prefix", "selector", "exact_alternatives"}
        ),
        "fragments": frozenset({"kind", "fragments", "exact_alternatives"}),
        "h7129_metadata": frozenset({"kind", "exact_alternatives"}),
        "fields": frozenset(
            {
                "kind",
                "allowed_prefixes",
                "expected_fields",
                "exact_alternatives",
            }
        ),
    }
)


def _parse_matcher(value: Any, *, path: str) -> MatcherDefinition:
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must be an object")
    kind = _string(value.get("kind"), path=f"{path}.kind")
    allowed = _MATCHER_FIELDS.get(kind)
    if allowed is None:
        raise ProfileError(f"{path}.kind uses unknown matcher {kind!r}")
    _require_fields(value, required={"kind"}, allowed=set(allowed), path=path)
    raw_exact_alternatives = _array(
        value.get("exact_alternatives", []),
        path=f"{path}.exact_alternatives",
    )
    exact_alternatives = tuple(
        _hex_bytes(item, path=f"{path}.exact_alternatives[{index}]", frame=True)
        for index, item in enumerate(raw_exact_alternatives)
    )
    prefix = (
        _hex_bytes(value["prefix"], path=f"{path}.prefix") if "prefix" in value else b""
    )
    selector = (
        _hex_bytes(value["selector"], path=f"{path}.selector")
        if "selector" in value
        else b""
    )
    exact = (
        _hex_bytes(value["exact"], path=f"{path}.exact", frame=True)
        if "exact" in value
        else b""
    )
    fragments = tuple(_array(value.get("fragments", []), path=f"{path}.fragments"))
    allowed_values = tuple(
        _array(value.get("allowed_values", []), path=f"{path}.allowed_values")
    )
    raw_allowed_prefixes = _array(
        value.get("allowed_prefixes", []),
        path=f"{path}.allowed_prefixes",
    )
    allowed_prefixes = tuple(
        _hex_bytes(item, path=f"{path}.allowed_prefixes[{index}]")
        for index, item in enumerate(raw_allowed_prefixes)
    )
    raw_fields = _array(
        value.get("expected_fields", []),
        path=f"{path}.expected_fields",
    )
    if any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) for part in item)
        for item in raw_fields
    ):
        raise ProfileError(f"{path}.expected_fields must contain integer pairs")
    expected_fields = tuple((item[0], item[1]) for item in raw_fields)
    for field_name, integers, minimum, maximum in (
        ("fragments", fragments, 0, 255),
        ("allowed_values", allowed_values, 0, 255),
    ):
        if any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or not minimum <= item <= maximum
            for item in integers
        ):
            raise ProfileError(f"{path}.{field_name} must contain bytes")
    if any(
        not 0 <= offset <= 18 or not 0 <= expected <= 255
        for offset, expected in expected_fields
    ):
        raise ProfileError(f"{path}.expected_fields contains an unsafe offset/value")
    required_by_kind = {
        "exact": bool(exact),
        "value_byte": bool(prefix) and bool(allowed_values),
        "zero_payload": bool(prefix),
        "prefix": bool(prefix),
        "prefix_selector": bool(prefix) and bool(selector),
        "fragments": bool(fragments),
        "h7129_metadata": True,
        "fields": bool(allowed_prefixes) and bool(expected_fields),
    }
    if not required_by_kind[kind]:
        raise ProfileError(f"{path} is incomplete for matcher kind {kind!r}")
    return MatcherDefinition(
        kind=kind,
        prefix=prefix,
        selector=selector,
        exact=exact,
        exact_alternatives=exact_alternatives,
        fragments=fragments,
        allowed_prefixes=allowed_prefixes,
        expected_fields=expected_fields,
        allowed_values=allowed_values,
    )


def _parse_profile(
    raw: Mapping[str, Any], *, lineage: tuple[str, ...], source: str
) -> DeviceProfile:
    root_fields = {
        "schema_version",
        "profile_id",
        "identity",
        "bluetooth",
        "channel",
        "protocol",
        "capabilities",
        "timings",
        "custom_auto_defaults",
    }
    _require_fields(raw, required=root_fields, allowed=root_fields, path="profile")
    schema_version = raw["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise ProfileError(f"unsupported schema_version {schema_version!r}")
    profile_id = _string(raw["profile_id"], path="profile.profile_id")
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfileError("profile.profile_id has an invalid format")

    identity = _object(raw["identity"], path="identity")
    _require_fields(
        identity,
        required={
            "manufacturer",
            "model",
            "display_name",
            "support_status",
            "advertised_name_prefixes",
        },
        allowed={
            "manufacturer",
            "model",
            "display_name",
            "support_status",
            "advertised_name_prefixes",
        },
        path="identity",
    )
    model_value = identity["model"]
    if model_value is not None and not isinstance(model_value, str):
        raise ProfileError("identity.model must be a model string or null")
    try:
        model = Model(model_value) if model_value is not None else None
        support_status = SupportStatus(identity["support_status"])
    except ValueError as err:
        raise ProfileError(f"invalid identity enum: {err}") from err
    prefixes = _string_tuple(
        identity["advertised_name_prefixes"], path="identity.advertised_name_prefixes"
    )
    if any(not prefix.strip() or prefix != prefix.strip() for prefix in prefixes):
        raise ProfileError(
            "identity advertised-name prefixes must be non-empty and unpadded"
        )

    bluetooth = _object(raw["bluetooth"], path="bluetooth")
    bluetooth_fields = {"service_uuid", "notify_uuid", "write_uuid"}
    _require_fields(
        bluetooth, required=bluetooth_fields, allowed=bluetooth_fields, path="bluetooth"
    )
    uuids: dict[str, str] = {}
    for field in bluetooth_fields:
        value = _string(bluetooth[field], path=f"bluetooth.{field}").lower()
        try:
            uuids[field] = str(UUID(value))
        except ValueError as err:
            raise ProfileError(f"bluetooth.{field} is not a valid UUID") from err
    if len(set(uuids.values())) != 3:
        raise ProfileError("service, notify, and write UUIDs must be distinct")

    channel = _object(raw["channel"], path="channel")
    _require_fields(
        channel,
        required={"strategy", "first_application_delay", "negotiation"},
        allowed={"strategy", "first_application_delay", "negotiation"},
        path="channel",
    )
    try:
        strategy = SecurityMode(channel["strategy"])
    except ValueError as err:
        raise ProfileError(f"unknown channel strategy {channel['strategy']!r}") from err
    first_application_delay = float(
        _number(
            channel["first_application_delay"],
            path="channel.first_application_delay",
            maximum=1.0,
        )
    )
    negotiation_raw = channel["negotiation"]
    negotiation: NegotiationPolicy | None
    if negotiation_raw is None:
        negotiation = None
    else:
        if not isinstance(negotiation_raw, dict):
            raise ProfileError("channel.negotiation must be an object or null")
        fields = {"attempts", "retry_interval", "phase_timeout", "step_delay"}
        _require_fields(
            negotiation_raw, required=fields, allowed=fields, path="channel.negotiation"
        )
        negotiation = NegotiationPolicy(
            attempts=int(
                _number(
                    negotiation_raw["attempts"],
                    path="channel.negotiation.attempts",
                    minimum=1,
                    maximum=MAX_ATTEMPTS,
                    integer=True,
                )
            ),
            retry_interval=float(
                _number(
                    negotiation_raw["retry_interval"],
                    path="channel.negotiation.retry_interval",
                    maximum=MAX_REQUEST_TIMEOUT,
                )
            ),
            phase_timeout=float(
                _number(
                    negotiation_raw["phase_timeout"],
                    path="channel.negotiation.phase_timeout",
                    minimum=0.001,
                    maximum=MAX_REQUEST_TIMEOUT,
                )
            ),
            step_delay=float(
                _number(
                    negotiation_raw["step_delay"],
                    path="channel.negotiation.step_delay",
                    maximum=1.0,
                )
            ),
        )
        if (
            negotiation.retry_interval * negotiation.attempts
            < negotiation.phase_timeout
        ):
            raise ProfileError(
                "channel negotiation phase timeout exceeds its bounded retry window"
            )
    if strategy is SecurityMode.PLAINTEXT and negotiation is not None:
        raise ProfileError("plaintext profiles forbid negotiation policy")
    if strategy is SecurityMode.H7129_SESSION and negotiation is None:
        raise ProfileError("encrypted profiles require negotiation policy")

    protocol = _object(raw["protocol"], path="protocol")
    protocol_fields = {
        "codec",
        "checksum",
        "frame_size",
        "auto_parameter",
        "startup_mode_strategy",
        "commands",
        "request_catalog",
        "initialization_order",
        "refresh_order",
        "essential_request",
        "periodic_request",
    }
    _require_fields(
        protocol, required=protocol_fields, allowed=protocol_fields, path="protocol"
    )
    if (
        protocol["codec"] != "govee_20_byte_v1"
        or protocol["checksum"] != "xor_0_18"
        or protocol["frame_size"] != 20
    ):
        raise ProfileError(
            "protocol codec/checksum/frame_size must select the reviewed "
            "20-byte strategy"
        )
    if protocol["startup_mode_strategy"] not in {
        "h7124_selector_00",
        "h7129_selector_pair",
    }:
        raise ProfileError("protocol.startup_mode_strategy is unknown")
    commands_raw = protocol["commands"]
    command_fields = {
        "power",
        "fan_mode",
        "night_light_power",
        "night_light_brightness",
        "night_light_color",
    }
    if not isinstance(commands_raw, dict):
        raise ProfileError("protocol.commands must be an object")
    _require_fields(
        commands_raw,
        required=command_fields,
        allowed=command_fields,
        path="protocol.commands",
    )
    registered_commands = {
        "power": ("power_bool_v1", b"\x33\x01"),
        "fan_mode": ("fan_mode_v1", b"\x3a\x05"),
        "night_light_power": ("night_light_power_v1", b"\x3a\x1b\x01\x01"),
        "night_light_brightness": (
            "night_light_brightness_v1",
            b"\x3a\x1b\x01\x02",
        ),
        "night_light_color": ("night_light_color_v1", b"\x3a\x1b\x05\x0d"),
    }
    commands: dict[str, CommandDefinition] = {}
    for command, raw_definition in commands_raw.items():
        path = f"protocol.commands.{command}"
        definition = _object(raw_definition, path=path)
        fields = {"strategy", "prefix"}
        _require_fields(
            definition,
            required=fields,
            allowed=fields,
            path=path,
        )
        expected_strategy, expected_prefix = registered_commands[command]
        strategy_name = _string(definition["strategy"], path=f"{path}.strategy")
        prefix = _hex_bytes(definition["prefix"], path=f"{path}.prefix")
        if strategy_name != expected_strategy or prefix != expected_prefix:
            raise ProfileError(
                f"{path} selects an unknown or unsafe command template"
            )
        commands[command] = CommandDefinition(strategy_name, prefix)
    catalog_raw = protocol["request_catalog"]
    if not isinstance(catalog_raw, list) or not catalog_raw:
        raise ProfileError("protocol.request_catalog must be a non-empty array")
    catalog: dict[str, RequestDefinition] = {}
    for index, item in enumerate(catalog_raw):
        path = f"protocol.request_catalog[{index}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{path} must be an object")
        _require_fields(
            item,
            required={"name", "frame", "response"},
            allowed={"name", "frame", "response"},
            path=path,
        )
        name = _string(item["name"], path=f"{path}.name")
        if not _REQUEST_NAME_RE.fullmatch(name) or name in catalog:
            raise ProfileError(f"{path}.name is invalid or duplicated: {name!r}")
        catalog[name] = RequestDefinition(
            name=name,
            frame=_hex_bytes(item["frame"], path=f"{path}.frame", frame=True),
            response=_parse_matcher(item["response"], path=f"{path}.response"),
        )
    initialization_order = _string_tuple(
        protocol["initialization_order"], path="protocol.initialization_order"
    )
    refresh_order = _string_tuple(
        protocol["refresh_order"], path="protocol.refresh_order"
    )
    essential_request = _string(
        protocol["essential_request"], path="protocol.essential_request"
    )
    periodic_request = _string(
        protocol["periodic_request"], path="protocol.periodic_request"
    )
    references = (
        set(initialization_order)
        | set(refresh_order)
        | {essential_request, periodic_request}
    )
    unresolved = references - catalog.keys()
    if unresolved:
        raise ProfileError(
            f"protocol request references are unresolved: {sorted(unresolved)}"
        )
    if len(initialization_order) != len(set(initialization_order)) or len(
        refresh_order
    ) != len(set(refresh_order)):
        raise ProfileError("protocol request orders cannot contain duplicates")
    if (
        periodic_request != essential_request
        or catalog[periodic_request].frame[:2] != b"\xaa\x01"
    ):
        raise ProfileError(
            "the sole periodic request must be the essential aa 01 descriptor"
        )
    auto_parameter = int(
        _number(
            protocol["auto_parameter"],
            path="protocol.auto_parameter",
            maximum=255,
            integer=True,
        )
    )

    capabilities = _object(raw["capabilities"], path="capabilities")
    capability_fields = {
        "power",
        "fan",
        "light",
        "pm25",
        "filter_life",
        "unsolicited_updates",
        "refresh",
    }
    _require_fields(
        capabilities,
        required=capability_fields,
        allowed=capability_fields,
        path="capabilities",
    )
    if any(not isinstance(capabilities[field], bool) for field in capability_fields):
        raise ProfileError("all capability values must be booleans")

    timings_raw = _object(raw["timings"], path="timings")
    timing_fields = set(TimingProfile.__dataclass_fields__)
    _require_fields(
        timings_raw, required=timing_fields, allowed=timing_fields, path="timings"
    )
    attempt_fields = {
        "connect_attempts",
        "initialization_attempts",
        "essential_initialization_max_batches",
        "periodic_poll_attempts",
        "refresh_attempts",
        "command_send_attempts",
    }
    threshold_fields = {
        "recovery_storm_failure_threshold",
        "recovery_storm_advertisement_threshold",
    }
    values: dict[str, int | float] = {}
    for field in timing_fields:
        maximum = MAX_STARTUP_TIMEOUT
        if field in attempt_fields:
            maximum = MAX_ATTEMPTS
        elif field == "connection_attempt_timeout":
            maximum = MAX_GATT_TIMEOUT
        elif field == "transaction_timeout":
            maximum = MAX_REQUEST_TIMEOUT
        elif field == "command_deadline":
            maximum = MAX_COMMAND_DEADLINE
        elif field == "startup_timeout":
            maximum = MAX_STARTUP_TIMEOUT
        elif field == "backoff_max":
            maximum = MAX_BACKOFF
        elif field == "recovery_storm_window":
            maximum = MAX_RECOVERY_WINDOW
        integer = field in attempt_fields | threshold_fields
        minimum = 1 if integer else 0
        values[field] = _number(
            timings_raw[field],
            path=f"timings.{field}",
            minimum=minimum,
            maximum=maximum,
            integer=integer,
        )
    timings = TimingProfile(**values)  # type: ignore[arg-type]
    if (
        timings.notification_subscribe_timeout > MAX_REQUEST_TIMEOUT
        or timings.gatt_write_timeout > MAX_REQUEST_TIMEOUT
    ):
        raise ProfileError("GATT subscribe/write timeout exceeds reviewed ceiling")
    if (
        timings.backoff_initial > timings.backoff_max
        or timings.recovery_storm_initial_floor > timings.recovery_storm_max_floor
    ):
        raise ProfileError("timing floors/ceilings are not ordered")

    custom = _object(raw["custom_auto_defaults"], path="custom_auto_defaults")
    custom_fields = {
        "modes",
        "pm25_boundaries",
        "upshift_confirmation_seconds",
        "downshift_delays_minutes",
    }
    _require_fields(
        custom,
        required=custom_fields,
        allowed=custom_fields,
        path="custom_auto_defaults",
    )
    try:
        modes = tuple(
            FanMode(item)
            for item in _string_tuple(
                custom["modes"], path="custom_auto_defaults.modes"
            )
        )
    except ValueError as err:
        raise ProfileError(
            f"custom_auto_defaults contains an unknown fan mode: {err}"
        ) from err
    expected_modes = (
        FanMode.SLEEP,
        FanMode.LOW,
        FanMode.MEDIUM,
        FanMode.HIGH,
        FanMode.TURBO,
    )
    if modes != expected_modes:
        raise ProfileError(
            "custom_auto_defaults.modes must be Sleep through Turbo in order"
        )
    boundaries = (
        tuple(custom["pm25_boundaries"])
        if isinstance(custom["pm25_boundaries"], list)
        else ()
    )
    delays = (
        tuple(custom["downshift_delays_minutes"])
        if isinstance(custom["downshift_delays_minutes"], list)
        else ()
    )
    if (
        len(boundaries) != 4
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 999
            for value in boundaries
        )
        or list(boundaries) != sorted(set(boundaries))
    ):
        raise ProfileError(
            "custom_auto_defaults.pm25_boundaries must be four strictly "
            "ascending values from 0 through 999"
        )
    if len(delays) != 4 or any(
        not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1440
        for value in delays
    ):
        raise ProfileError(
            "custom_auto_defaults.downshift_delays_minutes must contain four "
            "values from 0 through 1440"
        )
    upshift = int(
        _number(
            custom["upshift_confirmation_seconds"],
            path="custom_auto_defaults.upshift_confirmation_seconds",
            maximum=300,
            integer=True,
        )
    )

    canonical = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    fingerprint = hashlib.sha256(canonical).hexdigest()
    return DeviceProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=profile_id,
        lineage=lineage,
        source_basename=source,
        fingerprint=fingerprint,
        identity=IdentityProfile(
            manufacturer=_string(
                identity["manufacturer"], path="identity.manufacturer"
            ),
            model=model,
            display_name=_string(
                identity["display_name"], path="identity.display_name"
            ),
            support_status=support_status,
            advertised_name_prefixes=prefixes,
        ),
        bluetooth=BluetoothProfile(**uuids),
        channel=ChannelProfile(strategy, first_application_delay, negotiation),
        protocol=ProtocolProfile(
            codec="govee_20_byte_v1",
            checksum="xor_0_18",
            frame_size=20,
            auto_parameter=auto_parameter,
            startup_mode_strategy=protocol["startup_mode_strategy"],
            commands=MappingProxyType(commands),
            request_catalog=MappingProxyType(catalog),
            initialization_order=initialization_order,
            refresh_order=refresh_order,
            essential_request=essential_request,
            periodic_request=periodic_request,
        ),
        capabilities=CapabilityProfile(**capabilities),
        timings=timings,
        custom_auto_defaults=CustomAutoDefaults(modes, boundaries, upshift, delays),
    )


def load_profile_registry(directory: Path | None = None) -> ProfileRegistry:
    """Atomically decode, resolve, validate, and build all bundled profiles."""
    profile_dir = directory or _PROFILE_DIR
    schema = _read_json(profile_dir / "schema.json")
    _validate_schema_artifact(schema)
    sources: dict[str, dict[str, Any]] = {}
    for filename in PROFILE_FILENAMES:
        raw = _read_json(profile_dir / filename)
        _reject_secret_fields(raw)
        profile_id = raw.get("profile_id")
        expected_id = filename.removesuffix(".json")
        if profile_id != expected_id:
            raise ProfileError(
                f"{filename} declares profile_id {profile_id!r}, expected "
                f"{expected_id!r}"
            )
        source_schema_version = raw.get("schema_version")
        if (
            not isinstance(source_schema_version, int)
            or isinstance(source_schema_version, bool)
            or source_schema_version != SCHEMA_VERSION
        ):
            raise ProfileError(f"{filename} uses unsupported schema_version")
        if expected_id in ROOT_PROFILE_IDS:
            if "extends" in raw:
                raise ProfileError(
                    f"root profile {expected_id!r} cannot extend another profile"
                )
        else:
            expected_parent = EXACT_PROFILE_PARENTS[expected_id]
            if raw.get("extends") != expected_parent:
                raise ProfileError(
                    f"exact profile {expected_id!r} must extend {expected_parent!r}"
                )
        sources[expected_id] = raw

    resolved: dict[str, DeviceProfile] = {}
    for profile_id in ("default", "default-encrypted", "h7124", "h7129"):
        source = sources[profile_id]
        if profile_id in ROOT_PROFILE_IDS:
            effective = dict(source)
            lineage = (profile_id,)
        else:
            parent = EXACT_PROFILE_PARENTS[profile_id]
            overrides = {
                key: value for key, value in source.items() if key != "extends"
            }
            effective = _deep_merge(sources[parent], overrides)
            lineage = (profile_id, parent)
        effective.pop("extends", None)
        effective["profile_id"] = profile_id
        try:
            resolved[profile_id] = _parse_profile(
                effective,
                lineage=lineage,
                source=f"{profile_id}.json",
            )
        except ProfileError:
            raise
        except (KeyError, TypeError, OverflowError) as err:
            raise ProfileError(
                f"effective profile {profile_id!r} contains an invalid field type"
            ) from err

    for root_id in ROOT_PROFILE_IDS:
        root = resolved[root_id]
        if root.identity.model is not None or root.identity.advertised_name_prefixes:
            raise ProfileError(f"baseline profile {root_id!r} must not be discoverable")
    prefixes: dict[str, str] = {}
    for profile_id in EXACT_PROFILE_PARENTS:
        profile = resolved[profile_id]
        if (
            profile.identity.model is None
            or not profile.identity.advertised_name_prefixes
        ):
            raise ProfileError(f"exact profile {profile_id!r} lacks model identity")
        for prefix in profile.identity.advertised_name_prefixes:
            folded = prefix.casefold()
            for existing, owner in prefixes.items():
                if folded.startswith(existing) or existing.startswith(folded):
                    raise ProfileError(
                        "ambiguous advertised-name prefixes for "
                        f"{owner!r} and {profile_id!r}"
                    )
            prefixes[folded] = profile_id
    return ProfileRegistry(MappingProxyType(resolved))


def get_profile_registry() -> ProfileRegistry:
    """Return the one cached immutable registry for this Python process."""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = load_profile_registry()
    return _registry


async def async_get_profile_registry(hass: Any) -> ProfileRegistry:
    """Load/validate bundled JSON off the event loop, then use the process cache."""
    add_executor_job = getattr(hass, "async_add_executor_job", None)
    if add_executor_job is not None:
        return await add_executor_job(get_profile_registry)
    return await asyncio.to_thread(get_profile_registry)


def reset_profile_registry_for_tests() -> None:
    """Clear the process cache for isolated loader tests."""
    global _registry
    with _registry_lock:
        _registry = None
