"""Domain failures with stable meanings for API and worker adapters."""


class DomainError(Exception):
    """Base class for expected, user-visible domain failures."""


class InsufficientStorage(DomainError):
    """A storage transaction could not commit because capacity was exhausted."""


class StorageBusy(DomainError):
    """Database contention interrupted a storage transaction."""


class NotFound(DomainError):
    """The scoped resource does not exist or is not visible."""


class AccessDenied(DomainError):
    """The principal lacks membership or the required membership role."""


class Conflict(DomainError):
    """A state or uniqueness conflict prevents the requested operation."""


class BranchBusy(Conflict):
    """An active simulation writer currently owns the branch."""


class StaleVersion(Conflict):
    """The caller's expected branch version no longer matches."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Expected state version {expected}, current version is {actual}.")


class IdempotencyConflict(Conflict):
    """An idempotency key was reused for a different request body."""


class InvalidEvent(DomainError):
    """An event is unknown, malformed, or violates a domain invariant."""


class InsufficientResources(InvalidEvent):
    """A resource account cannot cover a requested transfer."""


class InvalidLease(Conflict):
    """A worker lease is expired, stale, or belongs to another branch/job."""
