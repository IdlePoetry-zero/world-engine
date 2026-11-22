import io
import json
import zipfile
from hashlib import sha256
from uuid import uuid4

import pytest

from world_engine.application.backups import WORLD_PACK_ARCHIVE_MEMBER, BackupApplication, BackupArchiveError
from world_engine.application.world_pack_import import WorldPackApplication
from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.errors import NotFound
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.storage.backup_import import BackupImportError, WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _world_package(title: str) -> bytes:
    player = WorldEntity(id=uuid4(), kind="character", name="Traveler")
    state = WorldState(player_id=player.id, entities={player.id: player})
    return write_world_pack(
        manifest=WorldPackManifest(pack_id="test.portable", version="1.0.0", title=title),
        template={"scenario": state.model_dump(mode="json"), "author_extension": {"notes": "source only"}},
    )


def _replace_source(backup: bytes, source: bytes | None) -> bytes:
    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(payload["manifest.json"])
    if source is None:
        del payload[WORLD_PACK_ARCHIVE_MEMBER]
        manifest["members"] = [row for row in manifest["members"] if row["path"] != WORLD_PACK_ARCHIVE_MEMBER]
    else:
        payload[WORLD_PACK_ARCHIVE_MEMBER] = source
        source_entry = next(row for row in manifest["members"] if row["path"] == WORLD_PACK_ARCHIVE_MEMBER)
        source_entry.update(size=len(source), sha256=sha256(source).hexdigest())
    payload["manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return output.getvalue()


@pytest.fixture
def repositories(tmp_path):
    source = Database(f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    restored = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    source.create_schema()
    restored.create_schema()
    try:
        yield WorldRepository(source), WorldRepository(restored)
    finally:
        restored.dispose()
        source.dispose()


@pytest.mark.parametrize(("format_version", "streaming"), [("1.0", False), ("2.0", True)])
def test_backup_preserves_exact_author_source_through_isolated_restore(repositories, format_version, streaming):
    source, restored = repositories
    source_owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    restored_owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    source_app = WorldPackApplication(WorldApplication(source, ContentCatalog()))
    restored_app = WorldPackApplication(WorldApplication(restored, ContentCatalog()))
    original = _world_package("World package archive")
    digest = sha256(original).hexdigest()
    created = source_app.import_world(source_owner, original, digest, "test-pack-backup")
    world_id = created["world_id"]
    backups = BackupApplication(source)
    encoded = backups.export_world(source_owner, world_id, format_version=format_version)
    with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
        assert archive.read(WORLD_PACK_ARCHIVE_MEMBER) == original
    def validate(data: bytes):
        if streaming:
            return backups.validate_archive_file_streaming(io.BytesIO(data))
        return backups.validate_archive(data)

    with pytest.raises(BackupArchiveError, match="failed archive validation"):
        validate(_replace_source(encoded, b"invalid nested file"))
    with pytest.raises(BackupArchiveError, match="does not match the genesis checkpoint"):
        validate(_replace_source(encoded, _world_package("Another package")))
    with pytest.raises(BackupArchiveError, match="source is missing"):
        validate(_replace_source(encoded, None))

    importer = WorldBackupImporter(restored)
    with validate(encoded) as verified:
        assert "world_pack_archive" not in verified.summary()
        tampered = verified.model_copy(update={"world_pack_archive": b"invalid nested file"})
        with pytest.raises(BackupImportError, match="invalid world package source"):
            importer.import_verified_backup(restored_owner, tampered)
        assert restored.list_worlds(restored_owner) == []
        importer.import_verified_backup(restored_owner, verified)

    assert restored_app.author_source(restored_owner, world_id) == original
    assert source_app.author_source(source_owner, world_id) == original
    assert restored.read_world(restored_owner, world_id).state.metadata["world_pack_archive_sha256"] == digest
    with pytest.raises(NotFound):
        restored_app.author_source(source_owner, world_id)
