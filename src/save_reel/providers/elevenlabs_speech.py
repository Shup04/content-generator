"""ElevenLabs V1 speech with timestamps; V2 voice discovery. No automatic retries."""

import base64
import binascii

import httpx
from pydantic import ValidationError

from save_reel.narration_models import SpeechAlignment, SpeechMetadata, SpeechSettings
from save_reel.providers.media import MediaError
from save_reel.providers.speech import GeneratedSpeech


class ElevenLabsSpeechProvider:
    base_url = "https://api.elevenlabs.io"

    def __init__(self, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        self.client = client

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                headers={"xi-api-key": self._api_key},
                timeout=180,
                follow_redirects=False,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise MediaError(
                f"ElevenLabs request failed (HTTP {exc.response.status_code}). "
                "Check the key's Text to Speech/Voices permissions, voice access, and credits."
            ) from None
        except httpx.RequestError:
            raise MediaError(
                "ElevenLabs connection error or timeout; no generation retry sent"
            ) from None

    def select_voice(self) -> tuple[str, str]:
        response = self._request("GET", "/v2/voices", params={"page_size": 100})
        try:
            voices = [
                v
                for v in response.json()["voices"]
                if v.get("category") == "premade" and v.get("voice_id")
            ]
            # Prefer a stock narrator; never clone, add, or purchase a voice.
            for name in ("George", "Brian", "Daniel", "Alice", "Sarah"):
                for voice in voices:
                    if voice.get("name", "").split(" - ")[0] == name:
                        return voice["voice_id"], voice["name"]
            if voices:
                return voices[0]["voice_id"], voices[0].get("name", "Default voice")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise MediaError("ElevenLabs returned an invalid voice list") from None
        raise MediaError("No default voice available. Set ELEVENLABS_VOICE_ID or pass --voice-id")

    def generate(self, text: str, settings: SpeechSettings) -> GeneratedSpeech:
        response = self._request(
            "POST",
            f"/v1/text-to-speech/{settings.voice_id}/with-timestamps",
            params={"output_format": "mp3_44100_128"},
            json={
                "text": text,
                "model_id": settings.model_id,
                "voice_settings": {
                    "stability": settings.stability,
                    "similarity_boost": settings.similarity_boost,
                    "speed": settings.speed,
                    "style": 0,
                    "use_speaker_boost": True,
                },
            },
        )
        try:
            body = response.json()
            content = base64.b64decode(body["audio_base64"], validate=True)
            alignment = SpeechAlignment.model_validate(
                body.get("normalized_alignment") or body.get("alignment")
            )
            if not content:
                raise ValueError("empty audio")
            cost = response.headers.get("character-cost")
            metadata = SpeechMetadata(
                alignment=alignment,
                request_id=response.headers.get("request-id"),
                character_cost=int(cost) if cost is not None else None,
            )
        except (ValueError, KeyError, TypeError, binascii.Error, ValidationError):
            raise MediaError(
                "ElevenLabs returned invalid audio or timestamps; no retry sent"
            ) from None
        return GeneratedSpeech(content, metadata)
