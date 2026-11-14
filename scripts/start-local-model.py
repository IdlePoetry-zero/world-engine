"""Reuse existing immutable Ollama blobs in an isolated project runtime.

No pull, deletion, or modification is performed in the source model directory.
Only new hard links and manifest copies are created under this project.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"E:\AI-demo\AI小说转漫画")
HOST = "http://127.0.0.1:11434"


def tags():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(HOST + "/api/tags", timeout=2) as response:
        return json.load(response)


def stage_models():
    source_models = SOURCE / ".models" / "ollama"
    destination = ROOT / ".runtime" / "ollama-models"
    (destination / "blobs").mkdir(parents=True, exist_ok=True)
    verified = set()
    for model in ("8b-q4_k_m", "14b-q4_k_m"):
        relative = Path("manifests-v2/ollama.com/library/qwen3") / model
        manifest_path = source_models / relative
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for layer in [manifest["config"], *manifest["layers"]]:
            digest = layer["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise RuntimeError("Invalid content digest in source model manifest")
            name = digest.replace(":", "-")
            source = source_models / "blobs" / name
            if source.stat().st_size != layer["size"]:
                raise RuntimeError("Source model blob size does not match manifest")
            if digest not in verified:
                with source.open("rb") as handle:
                    actual = hashlib.file_digest(handle, "sha256").hexdigest()
                if actual != digest[7:]:
                    raise RuntimeError("Source model blob integrity verification failed")
                verified.add(digest)
            target = destination / "blobs" / name
            if not target.exists():
                os.link(source, target)
            elif not os.path.samefile(source, target):
                raise RuntimeError("Unexpected independent file at staged model blob path")
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest), encoding="utf-8")
    return destination


def main():
    try:
        existing = tags()
    except (OSError, ValueError):
        existing = None
    if existing is not None:
        print(json.dumps({"status": "existing_service", "models": existing.get("models", [])}))
        return
    destination = stage_models()
    runtime = ROOT / ".runtime"
    profile, temporary, logs = runtime / "ollama-home", runtime / "ollama-tmp", runtime / "logs"
    for directory in (profile, temporary, logs):
        directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({"OLLAMA_MODELS": str(destination), "OLLAMA_HOST": "127.0.0.1:11434",
                        "USERPROFILE": str(profile), "TEMP": str(temporary), "TMP": str(temporary),
                        "NO_PROXY": "127.0.0.1,localhost"})
    for key in list(environment):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            environment.pop(key)
    with (logs / "ollama.stdout.log").open("ab") as out, (logs / "ollama.stderr.log").open("ab") as err:
        process = subprocess.Popen([str(SOURCE / ".runtime" / "ollama" / "ollama.exe"), "serve"],
                                   cwd=ROOT, env=environment, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    (runtime / "ollama-process.json").write_text(json.dumps({"pid": process.pid,
        "executable": str(SOURCE / ".runtime" / "ollama" / "ollama.exe"), "host": HOST}), encoding="utf-8")
    print(json.dumps({"status": "started", "pid": process.pid, "host": HOST}))


if __name__ == "__main__":
    main()
