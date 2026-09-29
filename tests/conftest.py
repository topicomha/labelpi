"""Shared test helpers. Tests never touch real printers."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import labelpi` works without installing it

EXAMPLE_CONFIG = ROOT / "config" / "printers.example.toml"

# A small valid config used as the starting point for many tests.
MINIMAL_TOML = """
[[printers]]
id = "tape"
type = "brother_pt"
address = "AA:BB:CC:DD:EE:FF"
dpi = 180

  [[printers.labels]]
  id = "tze-12"
  continuous = true
  tape_width_mm = 12
  print_height_px = 64
  margin_mm = 2

[[printers]]
id = "die"
type = "phomemo"
address = "11:22:33:44:55:66"
dpi = 203

  [[printers.labels]]
  id = "12x50"
  continuous = false
  width_mm = 12
  length_mm = 50

[[templates]]
id = "today"
text = "{date:%Y-%m-%d}"
"""


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
    """Every test runs as if LABELPI_MOCK=1."""
    monkeypatch.setenv("LABELPI_MOCK", "1")


@pytest.fixture
def write_config(tmp_path):
    """Write TOML text to a temp file and return its path."""

    def write(text: str) -> Path:
        path = tmp_path / "printers.toml"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    return write


@pytest.fixture
def config(write_config):
    from labelpi.config import load_config

    return load_config(write_config(MINIMAL_TOML))


@pytest.fixture
def tape_label(config):
    return config.printer("tape").label("tze-12")


@pytest.fixture
def die_label(config):
    return config.printer("die").label("12x50")
