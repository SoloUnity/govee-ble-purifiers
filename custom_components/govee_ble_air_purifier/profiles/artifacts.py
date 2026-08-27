"""Decode and resolve the closed set of bundled profile artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

from .errors import DuplicateProfileKeyError, ProfileError

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
PROFILE_DIR = Path(__file__).resolve().parent.parent / "model_profiles"

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


@dataclass(frozen=True, slots=True)
class EffectiveProfileDocument:
    """One inheritance-resolved profile ready for semantic parsing."""

    raw: Mapping[str, Any]
    lineage: tuple[str, ...]
    source: str


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


def _reject_secret_fields(value: Any, *, path: str = "$.") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _SECRET_FIELDS:
                raise ProfileError(f"forbidden secret/executable field at {path}{key}")
            _reject_secret_fields(child, path=f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, path=f"{path}{index}.")


def _deep_merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(parent)
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(cast(dict[str, Any], merged[key]), value)
        else:
            merged[key] = value
    return merged


def load_effective_documents(
    directory: Path | None = None,
) -> tuple[EffectiveProfileDocument, ...]:
    """Decode and inheritance-resolve every bundled artifact atomically."""
    profile_dir = directory or PROFILE_DIR
    _validate_schema_artifact(_read_json(profile_dir / "schema.json"))
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

    documents: list[EffectiveProfileDocument] = []
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
        documents.append(
            EffectiveProfileDocument(effective, lineage, f"{profile_id}.json")
        )
    return tuple(documents)
