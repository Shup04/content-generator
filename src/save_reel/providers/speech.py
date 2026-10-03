"""Speech provider contract independent of HTTP clients and local composition."""

from dataclasses import dataclass
from typing import Protocol

from save_reel.narration_models import SpeechMetadata, SpeechSettings
from save_reel.providers.media import MediaError


class SpeechRequestRejected(MediaError):
    """The provider explicitly rejected a request, so no audio was generated."""

    def __init__(self, message, *, status_code, code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.retryable = status_code == 429


@dataclass(frozen=True)
class GeneratedSpeech:
    content: bytes
    metadata: SpeechMetadata


class SpeechProvider(Protocol):
    def generate(self, text: str, settings: SpeechSettings) -> GeneratedSpeech: ...
