from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from world_engine.adapters.media.windows_speech import (
    WINDOWS_SPEECH_EVENT_TYPES,
    WindowsSpeechAdapter,
    WindowsSpeechArtifactExpired,
    WindowsSpeechOutputError,
    WindowsSpeechTimeout,
    register_windows_speech,
)
from world_engine.application.media_contracts import (
    AssetScope,
    AuthorizedMediaSource,
    MediaRegistry,
    MediaRequest,
)
from world_engine.domain.models import EventRecord, utc_now


def _wav_bytes(*, data=b"\x00\x00") -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 22_050, 44_100, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


class _FakeProcess:
    def __init__(self, environment, *, wav_bytes, delay_seconds=0):
        self.environment = environment
        self.wav_bytes = wav_bytes
        self.delay_seconds = delay_seconds
        self.received = None
        self.returncode = None
        self.was_killed = False
        self.started = asyncio.Event()

    async def communicate(self, *, input=None):
        self.received = input
        self.started.set()
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        Path(self.environment["WORLD_ENGINE_SPEECH_OUTPUT"]).write_bytes(self.wav_bytes)
        self.returncode = 0
        return b"", b""

    def kill(self):
        self.was_killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _FakeProcessFactory:
    def __init__(self, *, wav_bytes=None, delay_seconds=0):
        self.wav_bytes = wav_bytes if wav_bytes is not None else _wav_bytes()
        self.delay_seconds = delay_seconds
        self.calls = []
        self.processes = []

    async def __call__(self, *arguments, **options):
        process = _FakeProcess(
            options["env"], wav_bytes=self.wav_bytes, delay_seconds=self.delay_seconds
        )
        self.calls.append((arguments, options))
        self.processes.append(process)
        return process


def _source_request(text="你好，世界。", *, event_type="core.time_advanced.v1"):
    event = EventRecord(
        event_id=uuid4(),
        world_id=uuid4(),
        branch_id=uuid4(),
        sequence=1,
        tick=1,
        type=event_type,
        command_id=uuid4(),
        actor_id=uuid4(),
        ruleset_version="test.rules.v1",
        payload={"private": "raw payload must not reach PowerShell"},
        visibility={"policy": "public"},
        state_version_after=1,
        commit_boundary=True,
        committed_at=utc_now(),
    )
    return AuthorizedMediaSource(
        event=event,
        recipient_id=uuid4(),
        authorized_description=text,
        visibility=AssetScope(policy="public"),
    )


@pytest.mark.asyncio
async def test_registry_registration_sends_only_utf8_approved_text_to_fixed_powershell(tmp_path, monkeypatch):
    prompt = "你好，世界。 It is raining."
    factory = _FakeProcessFactory()
    monkeypatch.setenv("WORLD_ENGINE_DEEPSEEK_API_KEY", "must-not-be-forwarded")
    registry = MediaRegistry()
    adapter = register_windows_speech(
        registry,
        voice_name="Installed Test Voice",
        temporary_root=tmp_path / "speech-temp",
        process_factory=factory,
        maximum_asset_bytes=1024 * 1024,
        timeout_seconds=2,
    )
    source = _source_request(prompt)
    plan = registry.plan_requests(source)[0]

    artifact = await registry.generate_artifact(plan)

    assert artifact.data == _wav_bytes()
    assert artifact.receipt.content_type == "audio/wav"
    assert artifact.receipt.byte_size == len(artifact.data)
    assert WINDOWS_SPEECH_EVENT_TYPES == tuple(item.event_type for item in registry.manifests()[0].subscriptions)
    arguments, options = factory.calls[0]
    assert "-Command" in arguments
    assert prompt not in arguments
    assert b"private" not in factory.processes[0].received
    assert factory.processes[0].received == prompt.encode("utf-8")
    assert prompt not in str(options["env"])
    assert "WORLD_ENGINE_DEEPSEEK_API_KEY" not in options["env"]
    assert options["env"]["WORLD_ENGINE_SPEECH_VOICE"] == "Installed Test Voice"
    assert options["stdout"] == asyncio.subprocess.DEVNULL
    assert options["stderr"] == asyncio.subprocess.DEVNULL
    assert list((tmp_path / "speech-temp").iterdir()) == []
    await adapter.close()


@pytest.mark.asyncio
async def test_speech_adapter_rejects_oversized_wav_before_caching(tmp_path):
    factory = _FakeProcessFactory(wav_bytes=_wav_bytes(data=b"x" * 300))
    adapter = WindowsSpeechAdapter(
        process_factory=factory,
        temporary_root=tmp_path,
        maximum_asset_bytes=128,
    )

    with pytest.raises(WindowsSpeechOutputError, match="configured asset size"):
        await adapter.generate(MediaRequest(
            request_id=uuid4(),
            world_id=uuid4(),
            branch_id=uuid4(),
            source_event_ids=(uuid4(),),
            recipient_id=uuid4(),
            kind="speech",
            authorized_description="Bounded narration.",
            visibility=AssetScope(policy="public"),
            idempotency_key="test",
        ))
    assert adapter._pending_count == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_speech_adapter_timeout_kills_process_and_removes_temp_directory(tmp_path):
    factory = _FakeProcessFactory(delay_seconds=2)
    adapter = WindowsSpeechAdapter(
        process_factory=factory,
        temporary_root=tmp_path,
        maximum_asset_bytes=1024 * 1024,
        timeout_seconds=0.05,
    )
    request = MediaRequest(
        request_id=uuid4(), world_id=uuid4(), branch_id=uuid4(), source_event_ids=(uuid4(),),
        recipient_id=uuid4(), kind="speech", authorized_description="A timed narration.",
        visibility=AssetScope(policy="public"), idempotency_key="timeout",
    )

    with pytest.raises(WindowsSpeechTimeout):
        await adapter.generate(request)

    assert factory.processes[0].was_killed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_speech_adapter_cancellation_kills_process_and_cleans_temp_directory(tmp_path):
    factory = _FakeProcessFactory(delay_seconds=20)
    adapter = WindowsSpeechAdapter(
        process_factory=factory,
        temporary_root=tmp_path,
        maximum_asset_bytes=1024 * 1024,
        timeout_seconds=60,
    )
    request = MediaRequest(
        request_id=uuid4(), world_id=uuid4(), branch_id=uuid4(), source_event_ids=(uuid4(),),
        recipient_id=uuid4(), kind="speech", authorized_description="A cancelled narration.",
        visibility=AssetScope(policy="public"), idempotency_key="cancel",
    )
    task = asyncio.create_task(adapter.generate(request))
    while not factory.processes:
        await asyncio.sleep(0)
    await factory.processes[0].started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory.processes[0].was_killed
    assert adapter._pending_count == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unfetched_speech_artifacts_expire_and_release_memory(tmp_path):
    factory = _FakeProcessFactory()
    adapter = WindowsSpeechAdapter(
        process_factory=factory,
        temporary_root=tmp_path,
        maximum_asset_bytes=1024 * 1024,
        pending_ttl_seconds=1,
    )
    request = MediaRequest(
        request_id=uuid4(), world_id=uuid4(), branch_id=uuid4(), source_event_ids=(uuid4(),),
        recipient_id=uuid4(), kind="speech", authorized_description="An abandoned narration.",
        visibility=AssetScope(policy="public"), idempotency_key="expired",
    )
    receipt = await adapter.generate(request)
    assert adapter._pending_count == 1
    await asyncio.sleep(1.1)
    assert adapter._pending_count == 0
    with pytest.raises(WindowsSpeechArtifactExpired):
        await adapter.fetch(request, receipt, maximum_bytes=1024 * 1024)
