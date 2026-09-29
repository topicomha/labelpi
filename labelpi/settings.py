"""
Settings changed from the web page, saved in config/settings.json.

printers.toml is written by hand and only read. Anything the page can change
lives here instead: for now the templates; later label sizes, printer
addresses and calibration. Per-printer choices live under "printers":
{"brother": {"auto_feed": false}}. The file is gitignored - it's yours, not the repo's.

Templates: until the page saves anything, the list is the built-in starters
plus any [[templates]] in printers.toml (same id -> the config one wins). The
first add/edit/delete writes the whole list to settings.json, which from then
on is the only source.

Writes are atomic (write a temp file, then rename over the old one) so a power
cut mid-save can't leave a half-written file. A lock serialises changes from
waitress's request threads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from labelpi.templates import Template, TemplateError, validate_template

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_NAME = 40
MAX_TEMPLATES = 100


class SettingsStore:
    def __init__(self, path: Path, seed_templates: list[Template]):
        self.path = path
        self._seed = list(seed_templates)
        self._lock = threading.Lock()
        self._data = self._load()

    # --- per-printer settings --------------------------------------------------
    def auto_feed(self, printer_id: str) -> bool:
        """Feed each label out to the cutter when done (default), or chain them."""
        with self._lock:
            return bool(self._data.get("printers", {}).get(printer_id, {}).get("auto_feed", True))

    def set_auto_feed(self, printer_id: str, on: bool) -> None:
        with self._lock:
            printers = self._data.setdefault("printers", {})
            printers.setdefault(printer_id, {})["auto_feed"] = bool(on)
            self._write()

    # --- templates -------------------------------------------------------------
    def templates(self) -> list[Template]:
        with self._lock:
            return list(self._templates())

    def template(self, template_id: str) -> Template | None:
        return next((t for t in self.templates() if t.id == template_id), None)

    def add_template(self, name: str, text: str) -> Template:
        name = _clean_name(name)
        validate_template(text)
        with self._lock:
            templates = self._templates()
            if len(templates) >= MAX_TEMPLATES:
                raise TemplateError(f"there are already {MAX_TEMPLATES} templates")
            template = Template(_unique_id(name, {t.id for t in templates}), name, text)
            self._save_templates([*templates, template])
            return template

    def update_template(self, template_id: str, name: str, text: str) -> Template | None:
        """Returns None if there's no template with that id."""
        name = _clean_name(name)
        validate_template(text)
        with self._lock:
            templates = self._templates()
            if not any(t.id == template_id for t in templates):
                return None
            updated = Template(template_id, name, text)
            self._save_templates([updated if t.id == template_id else t for t in templates])
            return updated

    def delete_template(self, template_id: str) -> bool:
        with self._lock:
            templates = self._templates()
            remaining = [t for t in templates if t.id != template_id]
            if len(remaining) == len(templates):
                return False
            self._save_templates(remaining)
            return True

    # --- internals (call with the lock held) -------------------------------------
    def _templates(self) -> list[Template]:
        if "templates" not in self._data:
            return list(self._seed)
        return [Template(t["id"], t["name"], t["text"]) for t in self._data["templates"]]

    def _save_templates(self, templates: list[Template]) -> None:
        self._data["templates"] = [{"id": t.id, "name": t.name, "text": t.text} for t in templates]
        self._write()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            if "templates" in data:
                data["templates"] = _valid_templates(data["templates"])
            if not isinstance(data.get("printers", {}), dict):
                raise ValueError('"printers" is not an object')
            return data
        except (ValueError, KeyError, TypeError) as exc:
            # Don't lose the file and don't refuse to start: keep a copy aside.
            backup = self.path.with_name(f"{self.path.name}.broken-{datetime.now():%Y%m%d-%H%M%S}")
            self.path.replace(backup)
            log.error(
                "%s was unreadable (%s); moved it to %s and started fresh", self.path, exc, backup
            )
            return {"version": SCHEMA_VERSION}

    def _write(self) -> None:
        self._data["version"] = SCHEMA_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # make sure it's on the SD card before the rename
        os.replace(tmp, self.path)


def _valid_templates(items: Any) -> list[dict[str, str]]:
    """Keep the templates that are well-formed; log and drop the rest."""
    if not isinstance(items, list):
        raise ValueError('"templates" is not a list')
    good = []
    for item in items:
        try:
            validate_template(item["text"])
            good.append({"id": str(item["id"]), "name": str(item["name"]), "text": item["text"]})
        except (TemplateError, KeyError, TypeError) as exc:
            log.warning("dropping invalid saved template %r: %s", item, exc)
    return good


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TemplateError("a template needs a name")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise TemplateError(f"template names are at most {MAX_NAME} characters")
    return name


def _unique_id(name: str, taken: set[str]) -> str:
    """'Freezer bag!' -> 'freezer-bag', or 'freezer-bag-2' if that's taken."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate
