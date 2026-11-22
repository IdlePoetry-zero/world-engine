"""Compress one stopped, durable capacity probe using native NTFS compression."""

from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
from time import perf_counter
from uuid import UUID


PROBE = Path(__file__).with_name("probe-world-advance.py")


def allocated_bytes(path: Path) -> int:
    get_size = ctypes.WinDLL("kernel32", use_last_error=True).GetCompressedFileSizeW
    get_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_size.restype = wintypes.DWORD
    high = wintypes.DWORD()
    ctypes.set_last_error(0)
    low = get_size(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) | low


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def durable_cursor(db: sqlite3.Connection, world: UUID, branch: UUID, job: UUID) -> dict:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT j.status, j.fencing_token, j.committed_through_tick, "
        "b.head_seq, b.current_tick, b.state_version, b.state_hash, "
        "json_extract(j.result_json, '$.probe_pause') AS pause_code "
        "FROM world_jobs AS j JOIN world_branches AS b ON b.branch_id=j.branch_id "
        "WHERE j.world_id=? AND j.branch_id=? AND j.job_id=? AND j.job_type='simulation.world_advance'",
        (world.hex, branch.hex, job.hex),
    ).fetchone()
    if row is None:
        raise ValueError("The exact world-advance job is absent.")
    if row["committed_through_tick"] != row["current_tick"]:
        raise ValueError("The job cursor differs from the committed branch clock.")
    return dict(row)


def _probe_identity(directory: Path, job_id: UUID) -> tuple[UUID, UUID]:
    manifest_path = directory / ".probe-world-advance.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest_path.resolve().parent != directory or manifest["schema"] != 1 or UUID(manifest["job_id"]) != job_id:
        raise ValueError("The supplied job differs from the saved probe manifest.")
    return UUID(manifest["world_id"]), UUID(manifest["branch_id"])


def request_pause(directory: Path, job_id: UUID) -> dict:
    if os.name != "nt":
        raise ValueError("This command requires Windows NTFS file compression.")
    world, branch = _probe_identity(directory, job_id)
    path = (directory / "world-engine.db").resolve(strict=True)
    if path.parent != directory:
        raise ValueError("The database must be in the probe directory.")
    probe = runpy.run_path(str(PROBE))
    marker = directory / probe["MAINTENANCE_REQUEST_NAME"]
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)) as db:
        cursor = durable_cursor(db, world, branch, job_id)
    if cursor["status"] not in {"running", "queued"}:
        raise ValueError("Only an active or queued capacity job can receive a maintenance request.")
    if not probe["_maintenance_requested"](marker, job_id):
        probe["_atomic_json"](marker, {"schema": 1, "job_id": str(job_id)})
    return {"job_id": str(job_id), "maintenance_request": str(marker), "status": "requested"}


def compress(directory: Path, job_id: UUID, output: Path | None) -> dict:
    if os.name != "nt":
        raise ValueError("This command requires Windows NTFS file compression.")
    lock = directory / ".probe-world-advance.lock"
    path = directory / "world-engine.db"
    if any(not item.is_file() or item.resolve().parent != directory for item in (lock, path)):
        raise ValueError("A dedicated probe directory with a local lock, manifest, and database is required.")
    if output is not None and output.exists():
        raise FileExistsError("The report destination already exists.")

    probe = runpy.run_path(str(PROBE))
    with probe["_directory_lock"](lock):
        world, branch = _probe_identity(directory, job_id)
        marker = directory / probe["MAINTENANCE_REQUEST_NAME"]
        requested = probe["_maintenance_requested"](marker, job_id)
        uri = path.as_uri()
        with closing(sqlite3.connect(f"{uri}?mode=rw", uri=True, timeout=10)) as db:
            before = durable_cursor(db, world, branch, job_id)
            if before["status"] not in {"paused", "succeeded"}:
                raise ValueError("The exact world-advance job must be durably paused or succeeded.")
            checkpoint = tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if checkpoint not in {(0, 0, 0), (0, -1, -1)}:
                raise ValueError("The database did not reach a complete WAL checkpoint.")

        digest = file_hash(path)
        size_before = allocated_bytes(path)
        start = perf_counter()
        executable = Path(os.environ["SystemRoot"]) / "System32" / "compact.exe"
        subprocess.run(
            [str(executable), "/c", "/q", str(path)],
            check=True, timeout=600, capture_output=True,
        )
        compression_seconds = perf_counter() - start
        size_after = allocated_bytes(path)
        if file_hash(path) != digest:
            raise ValueError("The database byte hash changed during compression.")
        with closing(sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=10)) as db:
            if tuple(db.execute("PRAGMA integrity_check").fetchone()) != ("ok",):
                raise ValueError("SQLite integrity check failed.")
            if durable_cursor(db, world, branch, job_id) != before:
                raise ValueError("The job or branch cursor changed.")
        result = {
            "job_id": str(job_id), "world_id": str(world), "branch_id": str(branch),
            "cursor": before, "database_sha256": digest, "integrity_check": "ok",
            "database_hash_phase": "after_checkpoint_before_resume",
            "allocated_bytes_before": size_before, "allocated_bytes_after": size_after,
            "compression_seconds": compression_seconds,
        }
        if requested and before["pause_code"] == probe["MAINTENANCE_PAUSE"]:
            scope = probe["AccessScope"].model_validate_json((directory / probe["IDENTITY_NAME"]).read_text(encoding="utf-8"))
            database = probe["Database"](f"sqlite:///{path.as_posix()}")
            try:
                resumed = probe["WorldRepository"](database).control_job(scope, job_id, "resume")
                result["post_maintenance_status"] = resumed.status.value
            finally:
                database.dispose()
        if requested:
            marker.unlink()
        if output is not None:
            probe["_atomic_json"](output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", required=True, type=Path)
    parser.add_argument("--job-id", required=True, type=UUID)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-pause", action="store_true", help="ask the worker to stop at its next commit boundary")
    args = parser.parse_args()
    if args.request_pause and args.output is not None:
        parser.error("--output applies to compression, not to a pause request.")
    try:
        directory = args.data_directory.resolve(strict=True)
        result = request_pause(directory, args.job_id) if args.request_pause else compress(directory, args.job_id, args.output)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        parser.exit(1, f"Capacity maintenance failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
