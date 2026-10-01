"""OpenAI Image API adapter. No automatic retries of paid generation requests."""

import base64
import binascii
from io import BytesIO

from openai import APIConnectionError, APIStatusError, OpenAI
from PIL import Image, UnidentifiedImageError

from save_reel.media_models import ImageSettings
from save_reel.providers.media import GeneratedImage, MediaError


class OpenAIImageProvider:
    def __init__(self, client: OpenAI) -> None:
        self.client = client

    def generate(self, prompt: str, settings: ImageSettings) -> GeneratedImage:
        try:
            result = self.client.with_options(max_retries=0, timeout=300).images.generate(
                model=settings.image_model,
                prompt=prompt,
                size=settings.image_size,
                quality=settings.image_quality,
                output_format="png",
                background=settings.image_background,
                n=1,
            )
        except APIStatusError as exc:
            raise MediaError(
                f"OpenAI image request failed (HTTP {exc.status_code}, "
                f"request ID {exc.request_id or 'unavailable'}). Check API access and billing."
            ) from None
        except APIConnectionError:
            raise MediaError(
                "OpenAI image request had a connection error or timed out; "
                "it was not automatically retried."
            ) from None
        if not result.data or len(result.data) != 1 or not result.data[0].b64_json:
            raise MediaError("OpenAI did not return one base64-encoded image")
        try:
            content = base64.b64decode(result.data[0].b64_json, validate=True)
            with Image.open(BytesIO(content)) as image:
                expected_size = tuple(int(edge) for edge in settings.image_size.split("x"))
                if image.format != "PNG" or image.size != expected_size:
                    raise MediaError("OpenAI returned an unexpected image format or dimensions")
                image.verify()
            if settings.image_background == "transparent":
                with Image.open(BytesIO(content)) as image:
                    alpha_min, alpha_max = image.convert("RGBA").getchannel("A").getextrema()
                    if alpha_min != 0 or alpha_max == 0:
                        raise MediaError(
                            "OpenAI returned an image without a transparent background "
                            "or without visible content"
                        )
        except (binascii.Error, UnidentifiedImageError, OSError, ValueError):
            raise MediaError("OpenAI returned invalid image data") from None
        return GeneratedImage(
            content=content,
            request_id=getattr(result, "_request_id", None),
            usage=result.usage.model_dump(mode="json") if result.usage else {},
        )
