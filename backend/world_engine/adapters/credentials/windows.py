"""Windows Credential Manager adapter for exact, account-scoped lookups.

This module deliberately has no file, environment-variable, or plaintext
fallback. On non-Windows platforms the store reports unavailable and all
operations fail closed.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


_TARGET_PREFIX = "world-engine/"
_MAX_TARGET_LENGTH = 512
_MAX_SECRET_BYTES = 2560
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class CredentialStoreError(RuntimeError):
    """A safe, secret-free error from Windows Credential Manager."""

    def __init__(self, operation: str, error_code: int):
        self.operation = operation
        self.error_code = error_code
        super().__init__(f"Windows Credential Manager {operation} failed (error {error_code}).")


class CredentialStoreUnavailable(CredentialStoreError):
    """Raised when the current platform cannot provide OS credential storage."""

    def __init__(self):
        self.operation = "access"
        self.error_code = 0
        RuntimeError.__init__(self, "Windows Credential Manager is unavailable.")


class _CredentialAttributeW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttributeW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Read and write UTF-8 secrets using Windows Credential Manager.

    The adapter only addresses a caller-supplied target beginning with
    ``world-engine/``. It never enumerates credentials or retains secret
    values on the instance.
    """

    def __init__(self) -> None:
        self._api = None
        if os.name != "nt":
            return

        try:
            advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
            advapi32.CredWriteW.restype = wintypes.BOOL
            advapi32.CredReadW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.POINTER(_CredentialW)),
            ]
            advapi32.CredReadW.restype = wintypes.BOOL
            advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            advapi32.CredDeleteW.restype = wintypes.BOOL
            advapi32.CredFree.argtypes = [ctypes.c_void_p]
            advapi32.CredFree.restype = None
            self._api = advapi32
        except (AttributeError, OSError):
            self._api = None

    @property
    def available(self) -> bool:
        return self._api is not None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(available={self.available})"

    @staticmethod
    def _validate_target(target: str) -> None:
        if not isinstance(target, str):
            raise ValueError("Credential target must be text.")
        if (
            not target.startswith(_TARGET_PREFIX)
            or len(target) <= len(_TARGET_PREFIX)
            or len(target) > _MAX_TARGET_LENGTH
            or "\x00" in target
        ):
            raise ValueError("Credential target must use a valid world-engine/ scope.")
        try:
            target.encode("utf-16-le", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Credential target contains invalid text.") from exc

    @staticmethod
    def _encode_secret(value: str) -> bytearray:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Credential value must be non-empty text without NUL characters.")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Credential value is not valid UTF-8 text.") from exc
        if len(encoded) > _MAX_SECRET_BYTES:
            raise ValueError("Credential value exceeds the 2560-byte limit.")
        return bytearray(encoded)

    def _require_api(self):
        if self._api is None:
            raise CredentialStoreUnavailable()
        return self._api

    def set(self, target: str, value: str) -> None:
        self._validate_target(target)
        payload = self._encode_secret(value)
        try:
            api = self._require_api()
            blob = ctypes.create_string_buffer(bytes(payload), len(payload))
            credential = _CredentialW(
                Flags=0,
                Type=_CRED_TYPE_GENERIC,
                TargetName=target,
                Comment=None,
                LastWritten=wintypes.FILETIME(0, 0),
                CredentialBlobSize=len(payload),
                CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
                Persist=_CRED_PERSIST_LOCAL_MACHINE,
                AttributeCount=0,
                Attributes=None,
                TargetAlias=None,
                UserName=None,
            )
            if not api.CredWriteW(ctypes.byref(credential), 0):
                error_code = ctypes.get_last_error()
                raise CredentialStoreError("write", error_code)
        finally:
            if "blob" in locals():
                ctypes.memset(ctypes.addressof(blob), 0, ctypes.sizeof(blob))
            payload[:] = b"\x00" * len(payload)

    def get(self, target: str) -> str | None:
        self._validate_target(target)
        api = self._require_api()
        credential_ptr = ctypes.POINTER(_CredentialW)()
        if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr)):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError("read", error_code)

        payload = bytearray()
        try:
            credential = credential_ptr.contents
            if credential.CredentialBlobSize > _MAX_SECRET_BYTES:
                raise CredentialStoreError("read", 0)
            if credential.CredentialBlobSize:
                payload.extend(ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize))
            try:
                return payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise CredentialStoreError("decode", 0) from exc
        finally:
            payload[:] = b"\x00" * len(payload)
            api.CredFree(ctypes.cast(credential_ptr, ctypes.c_void_p))

    def delete(self, target: str) -> None:
        self._validate_target(target)
        api = self._require_api()
        if not api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_NOT_FOUND:
                return
            raise CredentialStoreError("delete", error_code)
