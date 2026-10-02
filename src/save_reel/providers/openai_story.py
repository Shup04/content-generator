"""OpenAI Responses structured output adapter. Imported only for paid text generation."""

from openai import APIError, OpenAI

from save_reel.providers.story import Response
from save_reel.story_models import StorySettings


class OpenAIStoryProvider:
    name = "openai"

    def __init__(self, settings: StorySettings, *, client=None):
        self.model = settings.model
        self.max_output_tokens = settings.max_output_tokens
        self.client = client or OpenAI(max_retries=0, timeout=180)

    def generate(
        self, *, stage: str, prompt: str, context: dict, response_type: type[Response]
    ) -> Response:
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[{"role": "developer", "content": prompt}],
                text_format=response_type,
                max_output_tokens=self.max_output_tokens,
                store=False,
            )
        except APIError as exc:
            # Do not echo server bodies, request headers, credentials or arbitrary upstream text.
            raise RuntimeError(
                f"OpenAI story request failed ({type(exc).__name__}); "
                "check model access, credentials and the saved run before resuming"
            ) from None
        if response.output_parsed is None:
            raise ValueError("OpenAI returned no structured story (refused or incomplete response)")
        return response.output_parsed
