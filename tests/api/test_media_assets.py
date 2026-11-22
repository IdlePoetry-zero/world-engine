import sqlite3
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.application.media_assets import (
    MediaAssetAccessDenied,
    MediaAssetAuth,
    MediaAssetConflict,
    MediaAssetIntegrityError,
    MediaAssetNotFound,
    MediaAssetStore,
    MediaAssetValidationError,
)
from world_engine.application.media_contracts import AssetScope, GeneratedMedia, MediaRequest
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.runtime import build_application, local_identity
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _png_bytes() -> bytes:
    def chunk(name: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _request(*, visibility=None, world_id=None, branch_id=None, request_id=None, recipient_id=None):
    return MediaRequest(
        request_id=request_id or uuid4(),
        world_id=world_id or uuid4(),
        branch_id=branch_id or uuid4(),
        source_event_ids=(uuid4(),),
        recipient_id=recipient_id,
        kind="image",
        authorized_description="A host-filtered local illustration.",
        visibility=visibility or AssetScope(policy="public"),
        idempotency_key=f"asset:{uuid4()}",
    )


def _receipt(request, data=None, *, content_type="image/png", request_id=None, byte_size=None, digest=None):
    data = data if data is not None else _png_bytes()
    return GeneratedMedia(
        request_id=request_id or request.request_id,
        content_hash=digest or sha256(data).hexdigest(),
        content_type=content_type,
        byte_size=byte_size if byte_size is not None else len(data),
    )


def _auth(request, *, user_id=None, tenant_id=None, role=MembershipRole.OWNER,
          verified_subject_ids=(), trusted_host=False, branch_id=None, world_id=None):
    return MediaAssetAuth(
        scope=AccessScope(tenant_id=tenant_id or uuid4(), user_id=user_id or uuid4()),
        world_id=world_id or request.world_id,
        branch_id=branch_id or request.branch_id,
        membership_role=role,
        verified_subject_ids=frozenset(verified_subject_ids),
        trusted_host=trusted_host,
    )


def _grant_count(store):
    with sqlite3.connect(store.index_path) as connection:
        return connection.execute("SELECT count(*) FROM media_asset_grants").fetchone()[0]


def test_store_is_idempotent_and_same_request_cannot_change_audience(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    request = _request()
    data = _png_bytes()
    receipt = _receipt(request, data)
    auth = _auth(request, role=MembershipRole.EDITOR)

    first = store.store(request, receipt, data, auth)
    repeated = store.store(request, receipt, data, auth)

    assert first == repeated
    assert store.lookup_request(request, auth) == first
    assert store.read(receipt.content_hash, auth) == data
    assert _grant_count(store) == 1
    blob = store._blob_directory(receipt.content_hash, create=False) / receipt.content_hash
    assert blob.read_bytes() == data
    assert not list(blob.parent.glob(".media-*.tmp"))

    participant_id = uuid4()
    widened_request = request.model_copy(update={
        "recipient_id": participant_id,
        "visibility": AssetScope(policy="participants", subject_ids=(participant_id,)),
    })
    with pytest.raises(MediaAssetConflict, match="already bound"):
        store.store(
            widened_request,
            receipt,
            data,
            auth.model_copy(update={"verified_subject_ids": frozenset({participant_id})}),
        )
    with pytest.raises(MediaAssetConflict, match="different request content or audience"):
        store.lookup_request(
            widened_request,
            auth.model_copy(update={"verified_subject_ids": frozenset({participant_id})}),
        )
    assert _grant_count(store) == 1


def test_concurrent_retries_create_one_grant_and_one_complete_blob(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    request = _request()
    data = _png_bytes()
    receipt = _receipt(request, data)
    auth = _auth(request, role=MembershipRole.OWNER)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.store(request, receipt, data, auth), range(16)))

    assert all(item == results[0] for item in results)
    assert _grant_count(store) == 1
    blob = store._blob_directory(receipt.content_hash, create=False) / receipt.content_hash
    assert blob.read_bytes() == data
    assert not list(blob.parent.glob(".media-*.tmp"))


def test_same_digest_keeps_separate_participant_grants_and_never_crosses_scope(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    data = _png_bytes()
    first_subject, second_subject, outsider = uuid4(), uuid4(), uuid4()
    first = _request(
        visibility=AssetScope(policy="participants", subject_ids=(first_subject,)),
        recipient_id=first_subject,
    )
    second = _request(
        visibility=AssetScope(policy="participants", subject_ids=(second_subject,)),
        world_id=first.world_id,
        branch_id=first.branch_id,
        recipient_id=second_subject,
    )
    digest = sha256(data).hexdigest()
    first_receipt, second_receipt = _receipt(first, data), _receipt(second, data)
    first_auth = _auth(first, role=MembershipRole.PLAYER, verified_subject_ids=(first_subject,))
    second_auth = _auth(
        second,
        tenant_id=first_auth.scope.tenant_id,
        role=MembershipRole.PLAYER,
        verified_subject_ids=(second_subject,),
    )

    store.store(first, first_receipt, data, first_auth)
    store.store(second, second_receipt, data, second_auth)

    assert store.read(digest, first_auth) == data
    assert store.read(digest, second_auth) == data
    outsider_auth = _auth(
        first,
        tenant_id=first_auth.scope.tenant_id,
        role=MembershipRole.OBSERVER,
        verified_subject_ids=(outsider,),
    )
    with pytest.raises(MediaAssetNotFound):
        store.read(digest, outsider_auth)
    observer_with_claimed_subject = outsider_auth.model_copy(
        update={"verified_subject_ids": frozenset({first_subject})}
    )
    with pytest.raises(MediaAssetNotFound):
        store.read(digest, observer_with_claimed_subject)

    other_tenant = _auth(
        first,
        role=MembershipRole.OWNER,
        verified_subject_ids=(first_subject,),
    )
    with pytest.raises(MediaAssetNotFound):
        store.read(digest, other_tenant)
    other_branch = _auth(
        first,
        tenant_id=first_auth.scope.tenant_id,
        role=MembershipRole.OWNER,
        verified_subject_ids=(first_subject,),
        branch_id=uuid4(),
    )
    with pytest.raises(MediaAssetNotFound):
        store.read(digest, other_branch)
    assert _grant_count(store) == 2
    assert len(list(store.blob_root.glob("*/*"))) == 1


def test_author_only_read_requires_creator_and_current_editor_role(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    request = _request(visibility=AssetScope(policy="author_only"))
    data = _png_bytes()
    receipt = _receipt(request, data)
    author_id, tenant_id = uuid4(), uuid4()
    author = _auth(request, user_id=author_id, tenant_id=tenant_id, role=MembershipRole.EDITOR)
    store.store(request, receipt, data, author)

    another_editor = _auth(request, tenant_id=tenant_id, role=MembershipRole.EDITOR)
    owner_same_user = _auth(request, user_id=author_id, tenant_id=tenant_id, role=MembershipRole.OWNER)
    player_same_user = _auth(request, user_id=author_id, tenant_id=tenant_id, role=MembershipRole.PLAYER)
    with pytest.raises(MediaAssetNotFound):
        store.read(receipt.content_hash, another_editor)
    with pytest.raises(MediaAssetNotFound):
        store.read(receipt.content_hash, player_same_user)
    assert store.read(receipt.content_hash, owner_same_user) == data


def test_write_requires_authorized_scope_and_verified_participant(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    public = _request()
    data = _png_bytes()
    receipt = _receipt(public, data)
    player = _auth(public, role=MembershipRole.PLAYER)
    with pytest.raises(MediaAssetAccessDenied):
        store.store(public, receipt, data, player)

    trusted_host = _auth(public, role=MembershipRole.PLAYER, trusted_host=True)
    store.store(public, receipt, data, trusted_host)
    assert store.read(receipt.content_hash, trusted_host) == data

    subject = uuid4()
    private = _request(visibility=AssetScope(policy="participants", subject_ids=(subject,)), recipient_id=subject)
    private_receipt = _receipt(private, data)
    without_subject = _auth(private, role=MembershipRole.PLAYER)
    with pytest.raises(MediaAssetAccessDenied, match="recipient"):
        store.store(private, private_receipt, data, without_subject)
    with_subject = _auth(private, role=MembershipRole.PLAYER, verified_subject_ids=(subject,))
    store.store(private, private_receipt, data, with_subject)
    assert store.read(private_receipt.content_hash, with_subject) == data


@pytest.mark.parametrize(
    "receipt_changes,data",
    [
        ({"byte_size": 4}, _png_bytes()),
        ({"content_hash": "0" * 64}, _png_bytes()),
        ({}, b"not a PNG image"),
        ({}, b"\x89PNG\r\n\x1a\nbad"),
        ({"request_id": uuid4()}, _png_bytes()),
        ({"content_type": "audio/wav"}, _png_bytes()),
    ],
)
def test_invalid_receipt_or_bytes_create_no_grant(tmp_path, receipt_changes, data):
    store = MediaAssetStore(tmp_path / "media")
    request = _request()
    receipt = _receipt(request, data)
    if "content_hash" in receipt_changes:
        # A mismatched hash is rejected before the declared bytes can be stored.
        receipt = receipt.model_copy(update={"content_hash": receipt_changes["content_hash"]})
        receipt_changes = {key: value for key, value in receipt_changes.items() if key != "content_hash"}
    if "request_id" in receipt_changes:
        receipt = receipt.model_copy(update={"request_id": receipt_changes["request_id"]})
        receipt_changes = {key: value for key, value in receipt_changes.items() if key != "request_id"}
    if receipt_changes:
        receipt = receipt.model_copy(update=receipt_changes)
    with pytest.raises(MediaAssetValidationError):
        store.store(request, receipt, data, _auth(request, role=MembershipRole.OWNER))
    assert _grant_count(store) == 0
    assert not list(store.blob_root.glob("*/*"))


def test_corrupt_blob_fails_closed_and_is_never_replaced(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    request = _request()
    data = _png_bytes()
    receipt = _receipt(request, data)
    auth = _auth(request, role=MembershipRole.OWNER)
    store.store(request, receipt, data, auth)

    path = store._blob_directory(receipt.content_hash, create=False) / receipt.content_hash
    path.write_bytes(b"corrupt bytes")
    with pytest.raises(MediaAssetIntegrityError):
        store.read(receipt.content_hash, auth)
    with pytest.raises(MediaAssetIntegrityError):
        store.lookup_request(request, auth)
    with pytest.raises(MediaAssetIntegrityError):
        store.store(request, receipt, data, auth)
    assert path.read_bytes() == b"corrupt bytes"


def test_read_rejects_hash_as_a_path_or_locator_without_exact_grant(tmp_path):
    store = MediaAssetStore(tmp_path / "media")
    request = _request()
    data = _png_bytes()
    receipt = _receipt(request, data)
    auth = _auth(request, role=MembershipRole.OWNER)
    store.store(request, receipt, data, auth)

    with pytest.raises(MediaAssetNotFound):
        store.read("../" + receipt.content_hash, auth)
    wrong_world = _auth(request, role=MembershipRole.OWNER, world_id=uuid4())
    with pytest.raises(MediaAssetNotFound):
        store.read(receipt.content_hash, wrong_world)


def test_asset_storage_does_not_mutate_authoritative_world_state(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'worlds.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    created = repository.create_world(scope, NewWorld(title="Media stays external", state=WorldState()))
    before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)

    request = _request(world_id=created.world.world_id, branch_id=created.branch.branch_id)
    data = _png_bytes()
    receipt = _receipt(request, data)
    auth = MediaAssetAuth(
        scope=scope,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        membership_role=MembershipRole.OWNER,
        trusted_host=True,
    )
    store = MediaAssetStore(tmp_path / "media")
    store.store(request, receipt, data, auth)

    after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    assert (after.state_hash, after.last_seq, after.branch.current_tick) == (
        before.state_hash,
        before.last_seq,
        before.branch.current_tick,
    )
    assert store.read(receipt.content_hash, auth) == data
    database.dispose()


def test_http_media_requires_matching_branch_and_participant(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    actor = WorldEntity(id=uuid4(), kind="character", name="Listener")
    outsider = WorldEntity(id=uuid4(), kind="character", name="Other listener")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        created = repository.create_world(scope, NewWorld(title="Media audience", state=WorldState(
            entities={actor.id: actor, outsider.id: outsider},
        )))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        request = _request(world_id=world_id, branch_id=branch_id, recipient_id=actor.id,
                           visibility=AssetScope(policy="participants", subject_ids=(actor.id,)))
        data = _png_bytes()
        receipt = _receipt(request, data)
        MediaAssetStore(tmp_path / "media").store(request, receipt, data, MediaAssetAuth(
            scope=scope, world_id=world_id, branch_id=branch_id, membership_role=MembershipRole.OWNER,
            verified_subject_ids=frozenset({actor.id}),
        ))
        url = f"/api/v1/worlds/{world_id}/branches/{branch_id}/media/{receipt.content_hash}"
        assert client.get(url).status_code == 404
        assert client.get(url, params={"actor_id": str(outsider.id)}).status_code == 404
        response = client.get(url, params={"actor_id": str(actor.id)})
        response.raise_for_status()
        assert response.content == data
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        child = repository.fork(scope, world_id, branch_id, 0, "Other branch")
        wrong_branch = url.replace(str(branch_id), str(child.branch_id))
        assert client.get(wrong_branch, params={"actor_id": str(actor.id)}).status_code == 404
