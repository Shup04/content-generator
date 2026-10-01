"""Speech provider contract independent of HTTP clients and local composition."""

from dataclasses import dataclass
from typing import Protocol

from save_reel.narration_models import SpeechMetadata, SpeechSettings


@dataclass(frozen=True)
class GeneratedSpeech:
    content: bytes
    metadata: SpeechMetadata


class SpeechProvider(Protocol):
    def generate(self, text: str, settings: SpeechSettings) -> GeneratedSpeech: ...
