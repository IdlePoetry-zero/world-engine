from __future__ import annotations

import hmac
import os
from secrets import token_urlsafe
from uuid import uuid4

import pytest

from world_engine.adapters.credentials import (
    CredentialStoreUnavailable,
    WindowsCredentialStore,
)


def test_target_and_value_limits_are_enforced_before_os_access():
    store = WindowsCredentialStore()

    with pytest.raises(ValueError):
        store.get("outside-world-engine/scope")
    with pytest.raises(ValueError):
        store.get("world-engine/")
    with pytest.raises(ValueError):
        store.get("world-engine/" + "x" * 512)
    with pytest.raises(ValueError):
        store.set("world-engine/test", "é" * 1281)
    with pytest.raises(ValueError):
        store.set("world-engine/test", "")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Credential Manager")
def test_windows_credential_round_trip_overwrite_and_delete():
    store = WindowsCredentialStore()
    if not store.available:
        pytest.skip("Windows Credential Manager is unavailable")

    target = f"world-engine/test/{uuid4()}"
    secret = token_urlsafe(40)
    replacement = token_urlsafe(40)
    try:
        store.set(target, secret)
        actual = store.get(target)
        assert actual is not None and hmac.compare_digest(actual, secret)

        store.set(target, replacement)
        actual = store.get(target)
        assert actual is not None and hmac.compare_digest(actual, replacement)

        store.delete(target)
        assert store.get(target) is None
        store.delete(target)
    finally:
        store.delete(target)


def test_non_windows_access_fails_closed_without_plaintext_fallback():
    store = WindowsCredentialStore()
    if os.name == "nt" and store.available:
        pytest.skip("Windows Credential Manager is available")

    assert store.available is False
    assert "available=False" in repr(store)
    with pytest.raises(CredentialStoreUnavailable):
        store.get("world-engine/test/no-fallback")
