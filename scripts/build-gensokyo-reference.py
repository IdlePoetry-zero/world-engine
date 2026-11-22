"""Build the separate Gensokyo reference archive for local import."""

import argparse
from hashlib import sha256
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from world_engine.content.gensokyo_reference import PACK_VERSION, build_archive  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=(
        PROJECT_ROOT / ".runtime" / "world-packs" / f"gensokyo-reference-{PACK_VERSION}.zip"
    ))
    args = parser.parse_args()
    archive = build_archive()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(archive)
    print(args.output.resolve())
    print(f"SHA-256 {sha256(archive).hexdigest()}")


if __name__ == "__main__":
    main()
