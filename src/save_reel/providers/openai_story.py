"""OpenAI Responses structured output adapter. Imported only for paid text generation."""

from openai import APIError, OpenAI

from save_reel.providers.story import Response
from save_reel.story_models import StorySettings


class OpenAIStoryProvider:
    name = "openai"

    def __init__(self, settings: StorySettings, *, client=None):
        self.model = settings.model
        self.max_output_tokens = settings.max_output_tokens
        self.settings = settings.model_copy(deep=True)
        self.client = client or OpenAI(max_retries=0, timeout=600)

    def generate(
        self, *, stage: str, prompt: str, context: dict, response_type: type[Response]
    ) -> Response:
        effort = self.settings.effort_for(stage)
        try:
            response = self.client.responses.parse(
                model=self.settings.model_for(stage),
                input=[{"role": "developer", "content": prompt}],
                text_format=response_type,
                max_output_tokens=self.max_output_tokens,
                store=False,
                **({"reasoning": {"effort": effort}} if effort else {}),
            )
        except APIError as exc:
            # Do not echo server bodies, request headers, credentials or arbitrary upstream text.
            raise RuntimeError(
                f"OpenAI story request failed ({type(exc).__name__}); "
                "check model access, credentials and the saved run before resuming"
            ) from None
        if response.output_parsed is None:
            # These fixed metadata fields explain token limits without logging response bodies.
            status = getattr(response, "status", "unknown")
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or "not supplied"
            tokens = getattr(getattr(response, "usage", None), "output_tokens", "unknown")
            raise ValueError(
                "OpenAI returned no structured story "
                f"(status={status}, reason={reason}, output_tokens={tokens})"
            )
        return response.output_parsed
