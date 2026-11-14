"""Explicit role routing and data-locality enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .contracts import DataPolicy, DepthProfile, ProviderConfig, RoleTask, SpeedProfile
from .errors import ProviderError, ProviderErrorCode


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    provider_id: str
    model_id: str | None = None


@dataclass(frozen=True, slots=True)
class RoleRoutingConfig:
    """Ordered targets are explicit primary/fallback choices for each role.

    Optional profile routes are keyed by `(role_task, speed, depth)`. They are
    exact overrides, not a dynamic model substitution policy.
    """

    routes: Mapping[RoleTask, tuple[ProviderTarget, ...]]
    profile_routes: Mapping[tuple[RoleTask, SpeedProfile, DepthProfile], tuple[ProviderTarget, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType({key: tuple(value) for key, value in self.routes.items()}))
        object.__setattr__(self, "profile_routes", MappingProxyType({key: tuple(value) for key, value in self.profile_routes.items()}))

    def resolve(
        self,
        role_task: RoleTask,
        *,
        speed: SpeedProfile,
        depth: DepthProfile,
        data_policy: DataPolicy,
        providers: Mapping[str, ProviderConfig],
    ) -> tuple[ProviderTarget, ...]:
        targets = self.profile_routes.get((role_task, speed, depth), self.routes.get(role_task, ()))
        allowed: list[ProviderTarget] = []
        for target in targets:
            provider = providers.get(target.provider_id)
            if provider is None:
                continue
            if data_policy is DataPolicy.LOCAL_ONLY and not provider.is_local_endpoint:
                continue
            allowed.append(target)
        if not allowed:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "No configured provider route is allowed by this data policy",
                retryable=False,
            )
        return tuple(allowed)


def default_deepseek_routing(provider_id: str = "deepseek") -> RoleRoutingConfig:
    """Development baseline from docs/05: route text tasks to deepseek-flash."""
    targets = (ProviderTarget(provider_id, "deepseek-flash"),)
    return RoleRoutingConfig(routes={
        role: targets
        for role in (
            RoleTask.INTENT,
            RoleTask.GM_ANSWER,
            RoleTask.NPC_DECISION,
            RoleTask.SOCIAL_ADJUDICATION,
            RoleTask.NARRATION,
            RoleTask.MEMORY_SUMMARY,
            RoleTask.AUTHOR_ASSIST,
        )
    })

