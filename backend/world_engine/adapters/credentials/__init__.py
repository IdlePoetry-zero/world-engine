"""Operating-system credential stores."""

from .windows import (
    CredentialStoreError,
    CredentialStoreUnavailable,
    WindowsCredentialStore,
)

__all__ = [
    "CredentialStoreError",
    "CredentialStoreUnavailable",
    "WindowsCredentialStore",
]
