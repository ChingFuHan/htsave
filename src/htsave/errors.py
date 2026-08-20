"""Domain errors with explicit fail-open boundaries."""


class HtsaveError(Exception):
    """Base class for recoverable htsave failures."""


class CorruptObjectError(HtsaveError):
    """A content-addressed object did not match its recorded digest."""


class DeltaError(HtsaveError):
    """A delta was malformed or could not recreate its target exactly."""


class TransportError(HtsaveError):
    """A versioned htsave transport frame was malformed or incompatible."""


class SecurityBoundaryError(HtsaveError):
    """A path or local state permission crossed an allowed boundary."""


class CompatibilityError(HtsaveError):
    """The active Codex contract is not supported safely."""
