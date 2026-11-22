import io
import json
import stat
import wave
import zipfile

import pytest

from world_engine.application.world_packs import (
    WorldPackArchiveError,
    WorldPackLimits,
    WorldPackManifest,
    load_world_pack,
    write_world_pack,
)


def _manifest(**overrides):
    values = {"pack_id": "author.harbor", "version": "1.2.0", "title": "Harbor"}
    return WorldPackManifest(**(values | overrides))


def _wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as sound:
        sound.setparams((1, 2, 8_000, 0, "NONE", "not compressed"))
        sound.writeframes(b"\x00\x00" * 8)
    return output.getvalue()


def _replace_manifest(data, **fields):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        manifest = json.loads(source.read("manifest.json")) | fields
        target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name in source.namelist():
            if name != "manifest.json":
                target.writestr(name, source.read(name))
    return output.getvalue()


def test_world_pack_preserves_inert_fields_and_declared_local_assets():
    assets = {"assets/潮声.wav": _wav()}
    template = {
        "base_pack_id": "modern_city",
        "base_pack_version": "1.7.0",
        "seed": 12,
        "future_system": {"setting": [1, {"name": "作者私设"}]},
    }
    archive = write_world_pack(manifest=_manifest(), template=template, assets=assets)
    verified = load_world_pack(_replace_manifest(archive, credits=["Example author"]))
    assert verified.template == {key: child for key, child in template.items() if key != "future_system"}
    assert verified.assets == assets
    assert verified.inert_metadata == {
        "manifest": {"credits": ["Example author"]},
        "template": {"future_system": template["future_system"]},
    }
    assert verified.compatibility_report["unsupported_fields"] == [
        {"section": "manifest", "field": "credits"},
        {"section": "template", "field": "future_system"},
    ]
    assert verified.compatibility_report["inert_asset_count"] == 1
    assert len(verified.manifest.content_hash) == 64


def test_world_pack_carries_a_scenario_without_a_catalog_background():
    opening = {"current_tick": 0, "metadata": {"private_notes": "\n 作者资料  "}}
    template = {"ruleset_version": "1.0.0", "scenario": opening}
    verified = load_world_pack(write_world_pack(manifest=_manifest(), template=template))
    assert verified.template == template


@pytest.mark.parametrize(
    "limits",
    [
        WorldPackLimits(compressed_bytes=100),
        WorldPackLimits(expanded_bytes=100),
        WorldPackLimits(member_bytes=100),
        WorldPackLimits(members=2),
    ],
)
def test_world_pack_rejects_limits_before_publishing(limits):
    data = write_world_pack(
        manifest=_manifest(), template={"base_pack_id": "modern_city"}, assets={"assets/water.wav": _wav()},
    )
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(data, limits=limits)


@pytest.mark.parametrize(
    "name",
    ["../water.wav", "assets/../water.wav", "assets\\water.wav", "assets/aux.wav", "assets/drawing.svg"],
)
def test_world_pack_rejects_unsafe_paths_and_non_media_members(name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("template.json", "{}")
        archive.writestr(name, _wav())
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(output.getvalue())


def test_world_pack_rejects_paths_that_alias_on_case_insensitive_filesystems():
    with pytest.raises(WorldPackArchiveError):
        write_world_pack(
            manifest=_manifest(), template={"base_pack_id": "modern_city"},
            assets={"assets/water.wav": _wav(), "assets/Water.wav": _wav()},
        )


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFREG | 0o755])
def test_world_pack_rejects_links_and_executable_permissions(mode):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("template.json", "{}")
        entry = zipfile.ZipInfo("assets/water.wav")
        entry.create_system = 3
        entry.external_attr = mode << 16
        archive.writestr(entry, _wav())
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(output.getvalue())


@pytest.mark.parametrize(
    "extension",
    [
        {"asset_ref": "https://example.invalid/image.png"},
        {"asset_ref": "//example.invalid/image.png"},
        {"script": "example disabled setting"},
        {"credentials": {"name": "example placeholder"}},
    ],
)
def test_world_pack_rejects_active_references_even_in_unsupported_fields(extension):
    with pytest.raises(WorldPackArchiveError):
        write_world_pack(
            manifest=_manifest(), template={"base_pack_id": "modern_city", "extension": extension},
        )


def test_world_pack_rejects_non_media_bytes_with_a_media_name():
    with pytest.raises(WorldPackArchiveError):
        write_world_pack(
            manifest=_manifest(), template={"base_pack_id": "modern_city"}, assets={"assets/water.wav": b"story"},
        )


def test_world_pack_verifies_manifest_and_content_digests():
    data = write_world_pack(manifest=_manifest(), template={"base_pack_id": "modern_city"})
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(_replace_manifest(data, content_hash="0" * 64))
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        original = source.read("template.json")
        target.writestr("template.json", original.replace(b"city", b"town"))
        target.writestr("manifest.json", source.read("manifest.json"))
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(output.getvalue())


def test_world_pack_can_inspect_an_engine_incompatible_archive_for_a_preview():
    data = write_world_pack(manifest=_manifest(), template={"base_pack_id": "modern_city"})
    data = _replace_manifest(data, engine_range=">=1.0.0,<2.0.0")
    with pytest.raises(WorldPackArchiveError) as exc:
        load_world_pack(data)
    assert exc.value.code == "WORLD_PACK_INCOMPATIBLE"
    verified = load_world_pack(data, engine_version=None)
    assert verified.manifest.supports_engine("1.1.0")
    assert not verified.manifest.supports_engine("0.1.0")
    with pytest.raises(WorldPackArchiveError):
        load_world_pack(_replace_manifest(data, schema_version="2.0"), engine_version=None)
