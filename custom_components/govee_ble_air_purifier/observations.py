"""Immutable, dependency-light semantic observation and provenance types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import FanMode


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """Immutable application frame with connection-scoped receipt evidence."""

    frame: bytes
    generation: int
    received_at: float


class CommandOrigin(StrEnum):
    """Owner that initiated an integration command."""

    HOME_ASSISTANT = "home_assistant"
    CUSTOM_AUTO = "custom_auto"
    HANDOFF = "handoff"


class ObservationSource(StrEnum):
    """Semantic authority that produced an observation."""

    STARTUP = "startup"
    COMMAND = "command"
    PHYSICAL = "physical"
    QUERY = "query"
    DEVICE = "device"


class ObservationPurpose(StrEnum):
    """Scheduler purpose active when an event was observed."""

    STARTUP = "startup"
    COMMAND = "command"
    ONE_SHOT = "one_shot"
    REFRESH = "refresh"
    PERIODIC = "periodic"
    UNSOLICITED = "unsolicited"


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationProvenance:
    """Common immutable provenance for one authoritative semantic event."""

    revision: int
    generation: int
    observed_at: float
    source: ObservationSource
    purpose: ObservationPurpose
    request_id: int | None = None
    operation_id: int | None = None
    command_origin: CommandOrigin | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FanModeObservation(ObservationProvenance):
    """One authoritative fan-mode event."""

    mode: FanMode


@dataclass(frozen=True, slots=True, kw_only=True)
class AirQualityObservation(ObservationProvenance):
    """One authoritative aa/ee-19 event, including invalid PM2.5 as ``None``."""

    pm25: int | None
    filter_life: int


type PurifierObservation = FanModeObservation | AirQualityObservation


__all__ = (
    "AirQualityObservation",
    "CommandOrigin",
    "FanModeObservation",
    "ObservationPurpose",
    "ObservationSource",
    "PurifierObservation",
    "ReceivedFrame",
)
