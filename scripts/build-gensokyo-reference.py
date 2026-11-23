"""Build the Gensokyo reference archive or the original station sample."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from world_engine.content.gensokyo_reference import (  # noqa: E402
    PACK_VERSION, build_archive, build_geography_index, build_reference_entries, build_source_index,
)
from world_engine.content import distant_station_story  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--station-demo", action="store_true", help="Build the original science-fiction example.")
    mode.add_argument("--character-cards", type=Path, help="Build using the author’s recovered character cards.")
    mode.add_argument("--playable-world", action="store_true", help="Build the canon-based playable world.")
    args = parser.parse_args()
    if args.station_demo:
        name = f"distant-station-{distant_station_story.PACK_VERSION}.zip"
        archive = distant_station_story.build_archive()
    elif args.playable_world:
        from world_engine.content import gensokyo_world

        name = f"gensokyo-world-{gensokyo_world.PACK_VERSION}.zip"
        archive = gensokyo_world.build_archive()
    elif args.character_cards:
        from world_engine.content.gensokyo_legacy_cast import CAST_VERSION, build_cast_archive

        name = f"gensokyo-reference-{CAST_VERSION}.zip"
        archive = build_cast_archive(args.character_cards)
    else:
        name = f"gensokyo-reference-{PACK_VERSION}.zip"
        archive = build_archive()
    output = args.output or PROJECT_ROOT / ".runtime" / "world-packs" / name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    print(output.resolve())
    print(f"SHA-256 {sha256(archive).hexdigest()}")
    if not args.station_demo:
        index = output.with_suffix(".index.json")
        if args.playable_world:
            with ZipFile(output) as package:
                scenarios = json.loads(package.read("template.json"))["story_openings"]
            data = {
                "entries": scenarios[-1]["scenario"]["world_book_entries"],
                "geography": gensokyo_world.world_geography_index(),
                "sources": gensokyo_world.world_source_index(),
                "openings": [{key: opening[key] for key in ("opening_id", "title", "position", "known_history")}
                             for opening in scenarios],
            }
        else:
            data = {"entries": [entry.model_dump(mode="json", exclude_none=True) for entry in build_reference_entries()],
                    "geography": build_geography_index(),
                    "sources": build_source_index()}
        index.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(index.resolve())


if __name__ == "__main__":
    main()
