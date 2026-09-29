from __future__ import annotations

import json
import threading

import pytest

from labelpi.settings import MAX_TEMPLATES, SettingsStore
from labelpi.templates import STARTER_TEMPLATES, Template, TemplateError

SEED = [Template("today", "Today's date", "{date:%d %b}"), Template("box", "Box", "{field:What}")]


@pytest.fixture
def path(tmp_path):
    return tmp_path / "settings.json"


@pytest.fixture
def store(path):
    return SettingsStore(path, SEED)


def test_starts_with_seed_and_writes_nothing(store, path):
    assert [t.id for t in store.templates()] == ["today", "box"]
    assert not path.exists()


def test_add_template_saves_everything(store, path):
    added = store.add_template("Freezer bag!", "{field:Item}\n{date:%d %b}")
    assert added.id == "freezer-bag"
    saved = json.loads(path.read_text())
    assert [t["id"] for t in saved["templates"]] == ["today", "box", "freezer-bag"]
    assert saved["version"] == 1


def test_changes_survive_a_restart(store, path):
    store.add_template("Jar", "{field:Contents}")
    store.delete_template("today")
    reopened = SettingsStore(path, SEED)
    assert [t.id for t in reopened.templates()] == ["box", "jar"]


def test_ids_are_unique(store):
    assert store.add_template("Box", "x").id == "box-2"
    assert store.add_template("Box", "y").id == "box-3"
    assert store.add_template("!!!", "z").id == "template"


def test_update_template(store):
    updated = store.update_template("box", "Big box", "{field:What}\n{date:%Y}")
    assert updated.name == "Big box" and updated.fields == ["What"]
    assert store.template("box").name == "Big box"


def test_update_and_delete_unknown(store):
    assert store.update_template("nope", "x", "x") is None
    assert store.delete_template("nope") is False


@pytest.mark.parametrize("name", ["", "   ", None, 5, "x" * 41])
def test_bad_names(store, name):
    with pytest.raises(TemplateError):
        store.add_template(name, "text")


def test_bad_template_text_is_rejected(store, path):
    with pytest.raises(TemplateError):
        store.add_template("Bad", "{dat:%Y}")
    assert not path.exists()  # nothing written


def test_template_limit(store):
    for i in range(MAX_TEMPLATES - len(SEED)):
        store.add_template(f"T{i}", "x")
    with pytest.raises(TemplateError, match="already"):
        store.add_template("One too many", "x")


def test_corrupt_file_is_moved_aside(path, caplog):
    path.write_text("{ not json", encoding="utf-8")
    store = SettingsStore(path, SEED)
    assert [t.id for t in store.templates()] == ["today", "box"]  # back to the seed
    backups = list(path.parent.glob("settings.json.broken-*"))
    assert len(backups) == 1 and backups[0].read_text() == "{ not json"
    assert "unreadable" in caplog.text


def test_invalid_saved_templates_are_dropped(path):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "templates": [
                    {"id": "good", "name": "Good", "text": "{date:%Y}"},
                    {"id": "bad", "name": "Bad", "text": "{nope}"},
                    {"id": "broken"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert [t.id for t in SettingsStore(path, SEED).templates()] == ["good"]


def test_no_temp_file_left_behind(store, path):
    store.add_template("Jar", "x")
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_concurrent_adds_are_all_kept(store):
    def add(i):
        store.add_template(f"Jar {i}", "x")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.templates()) == len(SEED) + 20


def test_real_starters_seed_cleanly(path):
    store = SettingsStore(path, list(STARTER_TEMPLATES))
    assert [t.id for t in store.templates()] == ["today", "opened", "food", "freezer", "container"]


def test_auto_feed_defaults_on_and_is_saved(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path, [])
    assert store.auto_feed("tape") is True
    store.set_auto_feed("tape", False)
    assert store.auto_feed("tape") is False and store.auto_feed("die") is True
    assert json.loads(path.read_text())["printers"] == {"tape": {"auto_feed": False}}
    assert SettingsStore(path, []).auto_feed("tape") is False  # survives a restart


def test_broken_printers_section_is_moved_aside(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"printers": []}')
    store = SettingsStore(path, [])
    assert store.auto_feed("tape") is True
    assert list(tmp_path.glob("settings.json.broken-*"))
