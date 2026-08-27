"""Errors raised while loading and selecting bundled model profiles."""


class ProfileError(ValueError):
    """Base error for an invalid bundled profile artifact."""


class DuplicateProfileKeyError(ProfileError):
    """Raised when JSON contains a duplicate object key."""


class ProfileSelectionError(ProfileError):
    """Raised when an exact model or advertised name cannot be selected safely."""
