"""Secret reference resolvers. Only references cross configuration boundaries."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Collection, Sequence
from typing import Protocol

from .errors import ProviderError, ProviderErrorCode


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


class EnvironmentSecretResolver:
    """Resolve `env:NAME` references from an explicit allowlist.

    The resolver performs one exact lookup and never enumerates the process
    environment. The application is responsible for choosing its allowlist.
    """

    _NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(
        self,
        allowed_names: Collection[str] = (),
        *,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._allowed = frozenset(allowed_names)
        self._environ = environ

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("env:"):
            raise ProviderError(ProviderErrorCode.SECRET, "Unsupported secret reference scheme")
        name = secret_ref[4:]
        if not self._NAME.fullmatch(name) or name not in self._allowed:
            raise ProviderError(ProviderErrorCode.SECRET, "Secret reference is not allowed")
        value = (self._environ if self._environ is not None else os.environ).get(name)
        if not value:
            raise ProviderError(ProviderErrorCode.SECRET, "Referenced environment secret is unavailable")
        return value


class OSCredentialSecretResolver:
    """Adapt an operating-system credential lookup without persisting values.

    `lookup` should read from the host's credential store for one exact target.
    No credential-store dependency is imposed on the model adapter package.
    """

    def __init__(self, lookup: Callable[[str], str | None], allowed_refs: Collection[str] = ()) -> None:
        self._lookup = lookup
        self._allowed = frozenset(allowed_refs)

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("os:"):
            raise ProviderError(ProviderErrorCode.SECRET, "Unsupported secret reference scheme")
        target = secret_ref[3:]
        if not target or target not in self._allowed:
            raise ProviderError(ProviderErrorCode.SECRET, "Secret reference is not allowed")
        value = self._lookup(target)
        if not value:
            raise ProviderError(ProviderErrorCode.SECRET, "Referenced OS credential is unavailable")
        return value


class CompositeSecretResolver:
    def __init__(self, resolvers: Sequence[SecretResolver]) -> None:
        self._resolvers = tuple(resolvers)

    def resolve(self, secret_ref: str) -> str:
        # Resolver implementations must refuse references they do not own.
        for resolver in self._resolvers:
            try:
                return resolver.resolve(secret_ref)
            except ProviderError as exc:
                if exc.safe_message == "Unsupported secret reference scheme":
                    continue
                raise
        raise ProviderError(ProviderErrorCode.SECRET, "No resolver accepts this secret reference")

