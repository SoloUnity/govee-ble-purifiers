"""Typed, profile-backed Custom Auto options shared by all integration layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .models import FanMode
from .profiles.types import CustomAutoDefaults

CONF_CUSTOM_AUTO_ENABLED: Final = "custom_auto_enabled"
CONF_CUSTOM_AUTO_PM25_BOUNDARIES: Final = "custom_auto_pm25_boundaries"
CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: Final = (
    "custom_auto_upshift_confirmation_seconds"
)
CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: Final = (
    "custom_auto_downshift_delays_minutes"
)


class CustomAutoOptionsError(ValueError):
    """Raised when stored or submitted Custom Auto options are invalid."""


@dataclass(frozen=True, slots=True)
class CustomAutoOptions:
    """One immutable set of effective per-entry Custom Auto settings."""

    enabled: bool
    modes: tuple[FanMode, ...]
    pm25_boundaries: tuple[int, int, int, int]
    upshift_confirmation_seconds: int
    downshift_delays_minutes: tuple[int, int, int, int]


def _integer(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CustomAutoOptionsError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise CustomAutoOptionsError(f"{name} must be between 0 and {maximum}")
    return value


def _four_integers(
    value: object, *, name: str, maximum: int
) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise CustomAutoOptionsError(f"{name} must contain exactly four values")
    parsed = tuple(
        _integer(item, name=f"{name}[{index}]", maximum=maximum)
        for index, item in enumerate(value)
    )
    return parsed  # type: ignore[return-value]


def parse_custom_auto_options(
    raw: Mapping[str, object], defaults: CustomAutoDefaults
) -> CustomAutoOptions:
    """Parse effective options, taking every absent setting from the profile."""
    enabled = raw.get(CONF_CUSTOM_AUTO_ENABLED, False)
    if not isinstance(enabled, bool):
        raise CustomAutoOptionsError(f"{CONF_CUSTOM_AUTO_ENABLED} must be a boolean")

    boundaries = _four_integers(
        raw.get(CONF_CUSTOM_AUTO_PM25_BOUNDARIES, list(defaults.pm25_boundaries)),
        name=CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
        maximum=999,
    )
    if any(
        left >= right
        for left, right in zip(boundaries, boundaries[1:], strict=False)
    ):
        raise CustomAutoOptionsError(
            f"{CONF_CUSTOM_AUTO_PM25_BOUNDARIES} must be strictly ascending"
        )

    upshift = _integer(
        raw.get(
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
            defaults.upshift_confirmation_seconds,
        ),
        name=CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
        maximum=300,
    )
    downshifts = _four_integers(
        raw.get(
            CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
            list(defaults.downshift_delays_minutes),
        ),
        name=CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
        maximum=1440,
    )
    return CustomAutoOptions(
        enabled=enabled,
        modes=defaults.modes,
        pm25_boundaries=boundaries,
        upshift_confirmation_seconds=upshift,
        downshift_delays_minutes=downshifts,
    )
