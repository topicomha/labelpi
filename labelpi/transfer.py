"""
Export templates to a file and import them again (here or on another Pi).

The file is JSON, readable and hand-editable:

    {
      "format": "labelpi-templates",
      "version": 1,
      "exported": "2026-09-28T21:30:00",
      "templates": [
        {"name": "Freezer", "layout": {...}},     # or "text": "..." for plain ones
        ...
      ],
      "pictures": {"<id>": "<base64 PNG>", ...}  # every picture those layouts use
    }

Pictures travel inside the file so an import works on a Pi that has never
seen them. On import they are stored again and may get new ids (the id is a
hash of the stored PNG), so the imported layouts are rewritten to match.
Pictures are stored before the templates are checked, so a rejected import
can leave a picture behind - harmless (same picture = same file) and it can
be deleted from the picture list.
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
from datetime import datetime
from typing import Any

from PIL import Image, UnidentifiedImageError

from labelpi.assets import AssetStore
from labelpi.layout import layout_assets
from labelpi.templates import Template, TemplateError

FORMAT = "labelpi-templates"
VERSION = 1


def export_templates(templates: list[Template], assets: AssetStore) -> dict[str, Any]:
    """Everything needed to recreate `templates` elsewhere, as a JSON-ready dict."""
    items: list[dict[str, Any]] = []
    pictures: dict[str, str] = {}
    for template in templates:
        if template.layout is None:
            items.append({"name": template.name, "text": template.text})
            continue
        items.append({"name": template.name, "layout": template.layout})
        for asset_id in sorted(layout_assets(template.layout)):
            if asset_id not in pictures and assets.exists(asset_id):
                data = assets.path(asset_id).read_bytes()
                pictures[asset_id] = base64.b64encode(data).decode("ascii")
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": datetime.now().isoformat(timespec="seconds"),
        "templates": items,
        "pictures": pictures,
    }


def read_import(data: Any, assets: AssetStore) -> list[tuple[str, Any, Any]]:
    """
    Check an export file's contents, store its pictures, and return the
    templates as (name, text, layout) with picture ids rewritten - ready for
    SettingsStore.import_templates(), which validates each template.
    TemplateError if the file isn't a labelpi export.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TemplateError("this isn't a labelpi templates file")
    if data.get("version") != VERSION:
        raise TemplateError(f"unsupported templates file version {data.get('version')!r}")
    templates = data.get("templates")
    pictures = data.get("pictures", {})
    if not isinstance(templates, list) or not templates:
        raise TemplateError("the file has no templates")
    if not isinstance(pictures, dict):
        raise TemplateError('"pictures" must be an object')

    items = []
    for n, item in enumerate(templates, 1):
        if not isinstance(item, dict):
            raise TemplateError(f"template {n} must be an object")
        items.append((item.get("name"), item.get("text"), copy.deepcopy(item.get("layout"))))

    # Store only the pictures the templates use; map old id -> new id.
    used = set()
    for _, _, layout in items:
        if isinstance(layout, dict):
            used |= _asset_refs(layout)
    new_ids = {}
    for old_id in sorted(used):
        if old_id not in pictures:
            raise TemplateError(f'the file uses picture "{old_id}" but doesn\'t include it')
        new_ids[old_id] = assets.add(_decode_picture(old_id, pictures[old_id])).id
    for _, _, layout in items:
        if isinstance(layout, dict):
            _rewrite_asset_refs(layout, new_ids)
    return items


def _decode_picture(picture_id: str, encoded: Any) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(base64.b64decode(encoded, validate=True)))
        image.load()
        return image
    except (binascii.Error, TypeError, ValueError, UnidentifiedImageError, OSError) as exc:
        raise TemplateError(f'picture "{picture_id}" in the file is broken ({exc})') from None


def _asset_refs(layout: dict[str, Any]) -> set[str]:
    """Picture ids in a not-yet-validated layout (tolerates odd shapes)."""
    refs = set()
    background = layout.get("background")
    if isinstance(background, dict) and isinstance(background.get("image"), dict):
        asset = background["image"].get("asset")
        if isinstance(asset, str):
            refs.add(asset)
    for element in layout.get("elements") or []:
        if isinstance(element, dict) and element.get("type") == "image":
            if isinstance(element.get("asset"), str):
                refs.add(element["asset"])
    return refs


def _rewrite_asset_refs(layout: dict[str, Any], new_ids: dict[str, str]) -> None:
    background = layout.get("background")
    if isinstance(background, dict) and isinstance(background.get("image"), dict):
        image = background["image"]
        image["asset"] = new_ids.get(image.get("asset"), image.get("asset"))
    for element in layout.get("elements") or []:
        if isinstance(element, dict) and element.get("type") == "image":
            element["asset"] = new_ids.get(element.get("asset"), element.get("asset"))
