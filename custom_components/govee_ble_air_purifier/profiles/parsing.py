"""Structural and semantic parsing of inheritance-resolved profiles."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from ..frame import validate_frame
from ..models import FanMode, Model, SecurityMode
from .artifacts import SCHEMA_VERSION
from .errors import ProfileError
from .types import (
    BluetoothProfile,
    CapabilityProfile,
    ChannelProfile,
    CommandDefinition,
    CustomAutoDefaults,
    DeviceProfile,
    IdentityProfile,
    MatcherDefinition,
    NegotiationPolicy,
    ProtocolProfile,
    RequestDefinition,
    SupportStatus,
    TimingProfile,
)

MAX_ATTEMPTS: Final = 3
MAX_GATT_TIMEOUT: Final = 45.0
MAX_REQUEST_TIMEOUT: Final = 15.0
MAX_COMMAND_DEADLINE: Final = 120.0
MAX_STARTUP_TIMEOUT: Final = 300.0
MAX_BACKOFF: Final = 60.0
MAX_RECOVERY_WINDOW: Final = 300.0

_PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REQUEST_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})(?: [0-9a-f]{2})*$")


def _object(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must be an object")
    return value


def _array(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProfileError(f"{path} must be an array")
    return value


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
            {"kind", "allowed_prefixes", "expected_fields", "exact_alternatives"}
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
    raw_alternatives = _array(
        value.get("exact_alternatives", []), path=f"{path}.exact_alternatives"
    )
    exact_alternatives = tuple(
        _hex_bytes(item, path=f"{path}.exact_alternatives[{index}]", frame=True)
        for index, item in enumerate(raw_alternatives)
    )
    prefix = (
        _hex_bytes(value["prefix"], path=f"{path}.prefix")
        if "prefix" in value
        else b""
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
    raw_prefixes = _array(
        value.get("allowed_prefixes", []), path=f"{path}.allowed_prefixes"
    )
    allowed_prefixes = tuple(
        _hex_bytes(item, path=f"{path}.allowed_prefixes[{index}]")
        for index, item in enumerate(raw_prefixes)
    )
    raw_fields = _array(
        value.get("expected_fields", []), path=f"{path}.expected_fields"
    )
    if any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) for part in item)
        for item in raw_fields
    ):
        raise ProfileError(f"{path}.expected_fields must contain integer pairs")
    expected_fields = tuple((item[0], item[1]) for item in raw_fields)
    for field_name, integers in (
        ("fragments", fragments),
        ("allowed_values", allowed_values),
    ):
        if any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or not 0 <= item <= 255
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


def _parse_identity(value: Any) -> IdentityProfile:
    identity = _object(value, path="identity")
    fields = {
        "manufacturer",
        "model",
        "display_name",
        "support_status",
        "advertised_name_prefixes",
    }
    _require_fields(identity, required=fields, allowed=fields, path="identity")
    model_value = identity["model"]
    if model_value is not None and not isinstance(model_value, str):
        raise ProfileError("identity.model must be a model string or null")
    try:
        model = Model(model_value) if model_value is not None else None
        support_status = SupportStatus(identity["support_status"])
    except ValueError as err:
        raise ProfileError(f"invalid identity enum: {err}") from err
    prefixes = _string_tuple(
        identity["advertised_name_prefixes"],
        path="identity.advertised_name_prefixes",
    )
    if any(not prefix.strip() or prefix != prefix.strip() for prefix in prefixes):
        raise ProfileError(
            "identity advertised-name prefixes must be non-empty and unpadded"
        )
    return IdentityProfile(
        manufacturer=_string(identity["manufacturer"], path="identity.manufacturer"),
        model=model,
        display_name=_string(identity["display_name"], path="identity.display_name"),
        support_status=support_status,
        advertised_name_prefixes=prefixes,
    )


def _parse_bluetooth(value: Any) -> BluetoothProfile:
    bluetooth = _object(value, path="bluetooth")
    fields = {"service_uuid", "notify_uuid", "write_uuid"}
    _require_fields(bluetooth, required=fields, allowed=fields, path="bluetooth")
    uuids: dict[str, str] = {}
    for field in fields:
        raw = _string(bluetooth[field], path=f"bluetooth.{field}").lower()
        try:
            uuids[field] = str(UUID(raw))
        except ValueError as err:
            raise ProfileError(f"bluetooth.{field} is not a valid UUID") from err
    if len(set(uuids.values())) != 3:
        raise ProfileError("service, notify, and write UUIDs must be distinct")
    return BluetoothProfile(**uuids)


def _parse_channel(value: Any) -> ChannelProfile:
    channel = _object(value, path="channel")
    fields = {"strategy", "first_application_delay", "negotiation"}
    _require_fields(channel, required=fields, allowed=fields, path="channel")
    try:
        strategy = SecurityMode(channel["strategy"])
    except ValueError as err:
        raise ProfileError(f"unknown channel strategy {channel['strategy']!r}") from err
    first_delay = float(
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
        negotiation_fields = {
            "attempts",
            "retry_interval",
            "phase_timeout",
            "step_delay",
        }
        _require_fields(
            negotiation_raw,
            required=negotiation_fields,
            allowed=negotiation_fields,
            path="channel.negotiation",
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
    return ChannelProfile(strategy, first_delay, negotiation)


def _parse_protocol(value: Any) -> ProtocolProfile:
    protocol = _object(value, path="protocol")
    fields = {
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
    _require_fields(protocol, required=fields, allowed=fields, path="protocol")
    if (
        protocol["codec"] != "govee_20_byte_v1"
        or protocol["checksum"] != "xor_0_18"
        or protocol["frame_size"] != 20
    ):
        raise ProfileError(
            "protocol codec/checksum/frame_size must select the reviewed "
            "20-byte strategy"
        )
    startup_strategy = protocol["startup_mode_strategy"]
    if startup_strategy not in {"h7124_selector_00", "h7129_selector_pair"}:
        raise ProfileError("protocol.startup_mode_strategy is unknown")

    commands_raw = _object(protocol["commands"], path="protocol.commands")
    command_fields = {
        "power",
        "fan_mode",
        "night_light_power",
        "night_light_brightness",
        "night_light_color",
    }
    _require_fields(
        commands_raw,
        required=command_fields,
        allowed=command_fields,
        path="protocol.commands",
    )
    registered = {
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
        _require_fields(
            definition,
            required={"strategy", "prefix"},
            allowed={"strategy", "prefix"},
            path=path,
        )
        expected_strategy, expected_prefix = registered[command]
        strategy = _string(definition["strategy"], path=f"{path}.strategy")
        prefix = _hex_bytes(definition["prefix"], path=f"{path}.prefix")
        if strategy != expected_strategy or prefix != expected_prefix:
            raise ProfileError(f"{path} selects an unknown or unsafe command template")
        commands[command] = CommandDefinition(strategy, prefix)

    catalog_raw = protocol["request_catalog"]
    if not isinstance(catalog_raw, list) or not catalog_raw:
        raise ProfileError("protocol.request_catalog must be a non-empty array")
    catalog: dict[str, RequestDefinition] = {}
    for index, item in enumerate(catalog_raw):
        path = f"protocol.request_catalog[{index}]"
        item = _object(item, path=path)
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
    return ProtocolProfile(
        codec="govee_20_byte_v1",
        checksum="xor_0_18",
        frame_size=20,
        auto_parameter=auto_parameter,
        startup_mode_strategy=startup_strategy,
        commands=MappingProxyType(commands),
        request_catalog=MappingProxyType(catalog),
        initialization_order=initialization_order,
        refresh_order=refresh_order,
        essential_request=essential_request,
        periodic_request=periodic_request,
    )


def _parse_capabilities(value: Any) -> CapabilityProfile:
    capabilities = _object(value, path="capabilities")
    fields = {
        "power",
        "fan",
        "light",
        "pm25",
        "filter_life",
        "unsolicited_updates",
        "refresh",
    }
    _require_fields(
        capabilities, required=fields, allowed=fields, path="capabilities"
    )
    if any(not isinstance(capabilities[field], bool) for field in fields):
        raise ProfileError("all capability values must be booleans")
    return CapabilityProfile(**capabilities)


def _parse_timings(value: Any) -> TimingProfile:
    timings_raw = _object(value, path="timings")
    fields = set(TimingProfile.__dataclass_fields__)
    _require_fields(timings_raw, required=fields, allowed=fields, path="timings")
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
    for field in fields:
        maximum = MAX_STARTUP_TIMEOUT
        if field in attempt_fields:
            maximum = MAX_ATTEMPTS
        elif field == "connection_attempt_timeout":
            maximum = MAX_GATT_TIMEOUT
        elif field == "transaction_timeout":
            maximum = MAX_REQUEST_TIMEOUT
        elif field == "command_deadline":
            maximum = MAX_COMMAND_DEADLINE
        elif field == "backoff_max":
            maximum = MAX_BACKOFF
        elif field == "recovery_storm_window":
            maximum = MAX_RECOVERY_WINDOW
        integer = field in attempt_fields | threshold_fields
        values[field] = _number(
            timings_raw[field],
            path=f"timings.{field}",
            minimum=1 if integer else 0,
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
    return timings


def _parse_custom_auto(value: Any) -> CustomAutoDefaults:
    custom = _object(value, path="custom_auto_defaults")
    fields = {
        "modes",
        "pm25_boundaries",
        "upshift_confirmation_seconds",
        "downshift_delays_minutes",
    }
    _require_fields(
        custom,
        required=fields,
        allowed=fields,
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
            not isinstance(item, int)
            or isinstance(item, bool)
            or not 0 <= item <= 999
            for item in boundaries
        )
        or list(boundaries) != sorted(set(boundaries))
    ):
        raise ProfileError(
            "custom_auto_defaults.pm25_boundaries must be four strictly "
            "ascending values from 0 through 999"
        )
    if len(delays) != 4 or any(
        not isinstance(item, int)
        or isinstance(item, bool)
        or not 0 <= item <= 1440
        for item in delays
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
    return CustomAutoDefaults(modes, boundaries, upshift, delays)


def parse_profile(
    raw: Mapping[str, Any], *, lineage: tuple[str, ...], source: str
) -> DeviceProfile:
    """Build one immutable effective profile from validated closed sections."""
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

    canonical = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return DeviceProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=profile_id,
        lineage=lineage,
        source_basename=source,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
        identity=_parse_identity(raw["identity"]),
        bluetooth=_parse_bluetooth(raw["bluetooth"]),
        channel=_parse_channel(raw["channel"]),
        protocol=_parse_protocol(raw["protocol"]),
        capabilities=_parse_capabilities(raw["capabilities"]),
        timings=_parse_timings(raw["timings"]),
        custom_auto_defaults=_parse_custom_auto(raw["custom_auto_defaults"]),
    )
