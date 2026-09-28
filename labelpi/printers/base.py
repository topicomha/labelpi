"""
The interface every printer backend implements.

Backends only ever receive a finished image from render.py - they know
nothing about text. They connect for each job and disconnect afterwards:
the printers go to sleep, and stale connections are the main source of
flakiness.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from PIL import Image

from labelpi.config import LabelConfig, PrinterConfig


class PrinterUnavailable(Exception):
    """Printer off, out of range or not answering. API -> 503."""


class PrinterError(Exception):
    """Printer reported an error, or something unexpected broke. API -> 500."""


class Printer(ABC):
    """Base class (like an abstract class in C#) for one configured printer."""

    def __init__(self, config: PrinterConfig):
        self.config = config

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def display_name(self) -> str:
        return self.config.display_name

    @property
    def labels(self) -> tuple[LabelConfig, ...]:
        return self.config.labels

    @abstractmethod
    def prepare(self, image: Image.Image, label: LabelConfig) -> Image.Image:
        """Rotate/pad/convert a rendered label into exactly what this printer takes.
        Preview shows the result of this, so it must not have side effects."""

    @abstractmethod
    def print(self, image: Image.Image, label: LabelConfig) -> None:
        """Connect, send the prepared image, wait until done, disconnect.
        Raise PrinterUnavailable or PrinterError on failure."""

    def status(self) -> dict:
        """Best-effort status (battery, tape...). Empty if the backend has none."""
        return {}
