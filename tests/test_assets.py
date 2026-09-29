from __future__ import annotations

import pytest
from PIL import Image

from labelpi import assets
from labelpi.assets import AssetError, AssetStore


@pytest.fixture
def store(tmp_path):
    return AssetStore(tmp_path / "assets")


def test_add_open_list_delete(store):
    info = store.add(Image.new("RGB", (40, 20), "black"))
    assert (info.width, info.height) == (40, 20)
    assert store.exists(info.id)
    assert store.open(info.id).mode == "L"
    assert [a.id for a in store.list()] == [info.id]
    assert store.delete(info.id)
    assert not store.exists(info.id) and store.list() == []
    assert not store.delete(info.id)


def test_same_image_twice_is_stored_once(store):
    first = store.add(Image.new("RGB", (10, 10), "red"))
    second = store.add(Image.new("RGB", (10, 10), "red"))
    assert first.id == second.id and len(store.list()) == 1


def test_big_images_are_shrunk_and_transparency_becomes_white(store):
    info = store.add(Image.new("RGBA", (3000, 1500), (0, 0, 0, 0)))
    assert (info.width, info.height) == (assets.MAX_SIDE_PX, assets.MAX_SIDE_PX // 2)
    assert store.open(info.id).getpixel((5, 5)) == 255


def test_unknown_or_malformed_ids(store):
    for bad in ("0" * 16, "../../etc/passwd", "ABC", ""):
        assert not store.exists(bad)
        with pytest.raises(AssetError):
            store.path(bad)


def test_limit(store, monkeypatch):
    monkeypatch.setattr(assets, "MAX_ASSETS", 1)
    store.add(Image.new("L", (5, 5), 0))
    with pytest.raises(AssetError, match="already 1"):
        store.add(Image.new("L", (5, 5), 255))
