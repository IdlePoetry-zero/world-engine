"""Build and preview the portable 回灯之约 pack without opening the world library."""

import argparse
from hashlib import sha256
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from world_engine.application.world_pack_import import WorldPackApplication  # noqa: E402
from world_engine.application.worlds import WorldApplication  # noqa: E402
from world_engine.content import ContentCatalog  # noqa: E402
from world_engine.content.gensokyo_scenario import PACK_VERSION, build_archive  # noqa: E402
from world_engine.storage.database import Database  # noqa: E402
from world_engine.storage.repository import WorldRepository  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=(
        PROJECT_ROOT / ".runtime" / "world-packs" / f"gensokyo-returning-lanterns-{PACK_VERSION}.zip"
    ))
    args = parser.parse_args()

    archive = build_archive()
    database = Database("sqlite:///:memory:")
    try:
        service = WorldPackApplication(WorldApplication(WorldRepository(database), ContentCatalog()))
        preview = service.preview(archive)
    finally:
        database.dispose()
    if not preview["can_import"]:
        parser.exit(1, "\n".join(preview["blockers"]) + "\n")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    print(output)
    print(f"SHA-256 {sha256(archive).hexdigest()}")


if __name__ == "__main__":
    main()
