"""Stable public facade for bundled model profiles."""

from ..models import FanMode as FanMode
from ..models import Model as Model
from ..models import SecurityMode as SecurityMode
from .artifacts import EXACT_PROFILE_PARENTS as EXACT_PROFILE_PARENTS
from .artifacts import PROFILE_FILENAMES as PROFILE_FILENAMES
from .artifacts import ROOT_PROFILE_IDS as ROOT_PROFILE_IDS
from .artifacts import SCHEMA_VERSION as SCHEMA_VERSION
from .errors import DuplicateProfileKeyError as DuplicateProfileKeyError
from .errors import ProfileError as ProfileError
from .errors import ProfileSelectionError as ProfileSelectionError
from .parsing import MAX_ATTEMPTS as MAX_ATTEMPTS
from .parsing import MAX_BACKOFF as MAX_BACKOFF
from .parsing import MAX_COMMAND_DEADLINE as MAX_COMMAND_DEADLINE
from .parsing import MAX_GATT_TIMEOUT as MAX_GATT_TIMEOUT
from .parsing import MAX_RECOVERY_WINDOW as MAX_RECOVERY_WINDOW
from .parsing import MAX_REQUEST_TIMEOUT as MAX_REQUEST_TIMEOUT
from .parsing import MAX_STARTUP_TIMEOUT as MAX_STARTUP_TIMEOUT
from .registry import ProfileRegistry as ProfileRegistry
from .registry import async_get_profile_registry as async_get_profile_registry
from .registry import get_profile_registry as get_profile_registry
from .registry import load_profile_registry as load_profile_registry
from .registry import (
    reset_profile_registry_for_tests as reset_profile_registry_for_tests,
)
from .types import BluetoothProfile as BluetoothProfile
from .types import CapabilityProfile as CapabilityProfile
from .types import ChannelProfile as ChannelProfile
from .types import CommandDefinition as CommandDefinition
from .types import CustomAutoDefaults as CustomAutoDefaults
from .types import DeviceProfile as DeviceProfile
from .types import IdentityProfile as IdentityProfile
from .types import MatcherDefinition as MatcherDefinition
from .types import NegotiationPolicy as NegotiationPolicy
from .types import ProtocolProfile as ProtocolProfile
from .types import RequestDefinition as RequestDefinition
from .types import SupportStatus as SupportStatus
from .types import TimingProfile as TimingProfile

__all__ = (
    "BluetoothProfile",
    "CapabilityProfile",
    "ChannelProfile",
    "CommandDefinition",
    "CustomAutoDefaults",
    "DeviceProfile",
    "DuplicateProfileKeyError",
    "EXACT_PROFILE_PARENTS",
    "FanMode",
    "IdentityProfile",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF",
    "MAX_COMMAND_DEADLINE",
    "MAX_GATT_TIMEOUT",
    "MAX_RECOVERY_WINDOW",
    "MAX_REQUEST_TIMEOUT",
    "MAX_STARTUP_TIMEOUT",
    "MatcherDefinition",
    "Model",
    "NegotiationPolicy",
    "PROFILE_FILENAMES",
    "ProfileError",
    "ProfileRegistry",
    "ProfileSelectionError",
    "ProtocolProfile",
    "ROOT_PROFILE_IDS",
    "RequestDefinition",
    "SCHEMA_VERSION",
    "SecurityMode",
    "SupportStatus",
    "TimingProfile",
    "async_get_profile_registry",
    "get_profile_registry",
    "load_profile_registry",
    "reset_profile_registry_for_tests",
)
