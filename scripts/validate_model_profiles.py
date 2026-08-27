#!/usr/bin/env python3
"""Validate every bundled purifier model profile and print safe metadata."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from custom_components.govee_ble_air_purifier.profiles import (  # noqa: E402
    ProfileError,
    load_profile_registry,
)


def main() -> int:
    """Run atomic profile validation for local development and CI."""
    try:
        registry = load_profile_registry()
    except ProfileError as err:
        print(f"Model profile validation failed: {err}", file=sys.stderr)
        return 1
    for profile_id, profile in registry.profiles.items():
        print(
            f"{profile_id}: lineage={' -> '.join(profile.lineage)} "
            f"fingerprint={profile.fingerprint}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
