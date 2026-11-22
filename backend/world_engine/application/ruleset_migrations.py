"""Approved ruleset transitions for verified backup copies."""

from dataclasses import dataclass

from world_engine.rules.versions import LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION

from .backups import VerifiedWorldBackup
from .errors import ApplicationFailure


@dataclass(frozen=True)
class RulesetUpgrade:
    source_version: str
    target_version: str
    summary: str

    def bind_copy(self, backup: VerifiedWorldBackup) -> VerifiedWorldBackup:
        if backup.world.ruleset_version != self.source_version:
            raise ApplicationFailure("RULESET_MIGRATION_UNAVAILABLE", "此存档没有对应的规则升级路径。", status_code=422)
        # 1.1.0 changes future adjudication only. Preserve the archived events,
        # their recorded rule versions, and exact checkpoints. The importer
        # verifies them again inside the independent database transaction.
        return backup.model_copy(update={
            "world": backup.world.model_copy(update={"ruleset_version": self.target_version}),
        })


_UPGRADES = (
    RulesetUpgrade(LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION,
                   "规则 1.1.0 在禁用魔法的世界中拒绝之后的修炼突破；已有事件、时刻和存档状态会保留。"),
)


def available_ruleset_upgrades(source_version: str) -> tuple[RulesetUpgrade, ...]:
    return tuple(upgrade for upgrade in _UPGRADES if upgrade.source_version == source_version)


def require_ruleset_upgrade(source_version: str, target_version: str) -> RulesetUpgrade:
    for upgrade in available_ruleset_upgrades(source_version):
        if upgrade.target_version == target_version:
            return upgrade
    raise ApplicationFailure("RULESET_MIGRATION_UNAVAILABLE", "此存档没有对应的规则升级路径。", status_code=422)
