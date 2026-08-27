"""Atomic profile loading, exact selection, and process caching."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from ..models import Model
from .artifacts import EXACT_PROFILE_PARENTS, ROOT_PROFILE_IDS, load_effective_documents
from .errors import ProfileError, ProfileSelectionError
from .parsing import parse_profile
from .types import DeviceProfile


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    """One atomically loaded set of bundled profiles."""

    profiles: Mapping[str, DeviceProfile]

    def for_model(self, model: Model | str) -> DeviceProfile:
        """Resolve an existing config-entry model to its exact profile."""
        selected = {Model.H7124: "h7124", Model.H7129: "h7129"}[Model(model)]
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


def load_profile_registry(directory: Path | None = None) -> ProfileRegistry:
    """Atomically decode, resolve, validate, and build all bundled profiles."""
    resolved: dict[str, DeviceProfile] = {}
    for document in load_effective_documents(directory):
        profile_id = cast(str, document.raw["profile_id"])
        try:
            resolved[profile_id] = parse_profile(
                document.raw,
                lineage=document.lineage,
                source=document.source,
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


_registry_lock = threading.Lock()
_registry: ProfileRegistry | None = None


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
