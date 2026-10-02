"""Stage-level text boundary, separate from image, speech and video providers."""

from typing import Protocol, TypeVar

from save_reel.models import Model

Response = TypeVar("Response", bound=Model)


class StoryProvider(Protocol):
    name: str
    model: str

    def generate(
        self, *, stage: str, prompt: str, context: dict, response_type: type[Response]
    ) -> Response:
        """Generate typed creative variables; the workflow revalidates business constraints."""
        ...
