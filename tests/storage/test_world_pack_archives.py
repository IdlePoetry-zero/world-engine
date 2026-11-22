from uuid import uuid4

import pytest
from sqlalchemy import select

from world_engine.domain.errors import AccessDenied, Conflict, NotFound
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, WorldState
from world_engine.storage.database import Database
from world_engine.storage.models import BranchRow, MembershipRow, WorldPackArchiveRow, WorldRow
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def repository(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'worlds.sqlite').as_posix()}")
    database.create_schema()
    try:
        yield WorldRepository(database)
    finally:
        database.dispose()


def test_archive_is_preserved_for_authors_in_the_same_tenant(repository):
    owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    archive = b"\x50\x4b\x03\x04\x00\xff\x80"
    imported = repository.create_world(
        owner,
        NewWorld(title="Imported", state=WorldState()),
        world_pack_archive=archive,
    )
    ordinary = repository.create_world(owner, NewWorld(title="Ordinary", state=WorldState()))
    world_id = imported.world.world_id

    assert repository.read_world_pack_archive(owner, world_id) == archive
    assert repository.has_world_pack_archive(owner, world_id)
    assert repository.read_world_pack_archive(owner, ordinary.world.world_id) is None
    assert not repository.has_world_pack_archive(owner, ordinary.world.world_id)

    editor = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    repository.grant_membership(owner, world_id, editor.user_id, MembershipRole.EDITOR)
    assert repository.read_world_pack_archive(editor, world_id) == archive
    assert repository.has_world_pack_archive(editor, world_id)

    for role in (MembershipRole.PLAYER, MembershipRole.OBSERVER):
        member = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
        repository.grant_membership(owner, world_id, member.user_id, role)
        with pytest.raises(AccessDenied):
            repository.read_world_pack_archive(member, world_id)
        with pytest.raises(AccessDenied):
            repository.has_world_pack_archive(member, world_id)

    stranger = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    with pytest.raises(AccessDenied):
        repository.read_world_pack_archive(stranger, world_id)

    foreign_owner = AccessScope(tenant_id=uuid4(), user_id=owner.user_id)
    with pytest.raises(NotFound):
        repository.read_world_pack_archive(foreign_owner, world_id)
    with pytest.raises(NotFound):
        repository.has_world_pack_archive(foreign_owner, world_id)


def test_archive_write_failure_rolls_back_world_creation(repository):
    owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    draft = NewWorld(title="Imported", state=WorldState())
    with repository.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_archive BEFORE INSERT ON world_pack_archives "
            "BEGIN SELECT RAISE(ABORT, 'injected archive write failure'); END"
        )

    with pytest.raises(Conflict):
        repository.create_world(owner, draft, world_pack_archive=b"archive bytes")
    with repository.database.session_factory() as session:
        for table in (WorldRow, BranchRow, MembershipRow, WorldPackArchiveRow):
            assert session.scalar(select(table)) is None

    with repository.database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_archive")

    repository.create_world(owner, draft, world_pack_archive=b"archive bytes")
    assert repository.read_world_pack_archive(owner, draft.world_id) == b"archive bytes"
