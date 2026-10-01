"""Provider contracts for real images and asynchronous image-to-video jobs."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from save_reel.media_models import BrollSettings


class MediaError(RuntimeError):
    """A safe-to-display media error without credentials or raw provider responses."""


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VideoTask:
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    download_url: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ImageProvider(Protocol):
    def generate(self, prompt: str, settings: BrollSettings) -> GeneratedImage: ...


class VideoProvider(Protocol):
    def submit(self, image: bytes, prompt: str, settings: BrollSettings) -> str: ...

    def query(self, task_id: str) -> VideoTask: ...

    def download(self, url: str) -> bytes: ...
