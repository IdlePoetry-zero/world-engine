"""Build and verify a portable world-pack ZIP from an authored JSON source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from world_engine.application.authored_world_pack_builder import (  # noqa: E402
    AuthoredWorldPackBuildError,
    AuthoredWorldPackBuilder,
    loads_authored_world_pack,
    minimal_authored_world_pack,
)
from world_engine.application.world_pack_authoring import WorldPackAuthoringError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="UTF-8 JSON source with manifest and template")
    parser.add_argument("--output", type=Path, help="ZIP output path")
    parser.add_argument("--report", type=Path, help="Optional JSON verification report path")
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    parser.add_argument("--example", action="store_true", help="Print a minimal source JSON example and exit")
    args = parser.parse_args(argv)

    if args.example:
        if args.input or args.output or args.report or args.force:
            parser.error("--example cannot be combined with build options")
        print(json.dumps(minimal_authored_world_pack(), ensure_ascii=False, indent=2))
        return 0
    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --example is used")
    if args.input.resolve() == args.output.resolve():
        parser.error("input JSON and output ZIP must be different paths")
    if args.report and args.report.resolve() in {args.input.resolve(), args.output.resolve()}:
        parser.error("the report must use a path distinct from input and output")
    if args.output.exists() and not args.force:
        parser.error(f"output already exists; pass --force to replace it: {args.output}")

    try:
        source = args.input.read_text(encoding="utf-8")
        document = loads_authored_world_pack(source)
        built = AuthoredWorldPackBuilder().build(document)
    except (OSError, AuthoredWorldPackBuildError, WorldPackAuthoringError, ValueError) as error:
        print(_format_error(error), file=sys.stderr)
        return 2

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(built.archive)
        report = {
            "archive_path": str(args.output.resolve()),
            "archive_sha256": built.archive_sha256,
            **built.report,
        }
        serialized_report = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(serialized_report, encoding="utf-8", newline="\n")
        print(serialized_report, end="")
        return 0
    except OSError as error:
        print(f"无法写入输出文件：{error}", file=sys.stderr)
        return 2


def _format_error(error: Exception) -> str:
    path = getattr(error, "path", "")
    prefix = f"{path}: " if path else ""
    details = getattr(error, "field_errors", ())
    diagnostic_lines = []
    for item in details:
        if isinstance(item, dict):
            item_path = item.get("path", "")
            message = item.get("message") or item.get("type") or "无效字段"
            diagnostic_lines.append(f"  {item_path}: {message}")
    suffix = "\n" + "\n".join(diagnostic_lines) if diagnostic_lines else ""
    return f"世界包构建失败：{prefix}{error}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
