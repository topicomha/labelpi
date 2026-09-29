"""
Images uploaded for use in templates (backgrounds, logos), kept as files.

Each image is stored once as a greyscale PNG in config/assets/ (gitignored,
next to settings.json), named after a hash of its content: <id>.png. The same
picture uploaded twice gets the same id. Templates refer to an image by id.

On upload the image is flattened (transparency -> white), turned greyscale
and scaled down to at most MAX_SIDE_PX, since labels are tiny: a 40 mm label
at 203 dpi is ~320 px long. That keeps files and memory small on the Pi.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from labelpi.render import flatten_to_grey

MAX_SIDE_PX = 1200
MAX_ASSETS = 200
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class AssetError(ValueError):
    """Unknown asset, or too many of them. API -> 400/404."""


@dataclass(frozen=True)
class AssetInfo:
    id: str
    width: int
    height: int
    bytes: int


class AssetStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()

    def add(self, source: Image.Image) -> AssetInfo:
        """Normalise and save an image; returns its info (existing if a duplicate)."""
        grey = _normalise(source)
        buffer = io.BytesIO()
        grey.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        asset_id = hashlib.sha256(data).hexdigest()[:16]
        with self._lock:
            path = self._path(asset_id)
            if not path.exists():
                if len(self._ids()) >= MAX_ASSETS:
                    raise AssetError(f"there are already {MAX_ASSETS} images; delete some first")
                self.directory.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)  # atomic: never a half-written image
        return AssetInfo(asset_id, grey.width, grey.height, len(data))

    def exists(self, asset_id: str) -> bool:
        return bool(_ID_RE.match(asset_id)) and self._path(asset_id).exists()

    def path(self, asset_id: str) -> Path:
        """The PNG file for an asset id; AssetError if there isn't one."""
        if not self.exists(asset_id):
            raise AssetError(f'unknown image "{asset_id}"')
        return self._path(asset_id)

    def open(self, asset_id: str) -> Image.Image:
        """Load an asset as a greyscale ("L") image."""
        with Image.open(self.path(asset_id)) as image:
            return image.convert("L")

    def list(self) -> list[AssetInfo]:
        infos = []
        for asset_id in sorted(self._ids()):
            path = self._path(asset_id)
            with Image.open(path) as image:
                infos.append(AssetInfo(asset_id, image.width, image.height, path.stat().st_size))
        return infos

    def delete(self, asset_id: str) -> bool:
        if not self.exists(asset_id):
            return False
        self._path(asset_id).unlink()
        return True

    def _path(self, asset_id: str) -> Path:
        return self.directory / f"{asset_id}.png"

    def _ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return [p.stem for p in self.directory.glob("*.png") if _ID_RE.match(p.stem)]


def _normalise(source: Image.Image) -> Image.Image:
    """EXIF-rotate, put transparency on white, greyscale, shrink to MAX_SIDE_PX."""
    grey = flatten_to_grey(source)
    grey.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.Resampling.LANCZOS)  # keeps aspect
    return grey
