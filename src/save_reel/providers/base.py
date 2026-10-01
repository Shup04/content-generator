from typing import Protocol

from save_reel.models import ConceptRequest, ReelConcept


class ConceptProvider(Protocol):
    """Boundary for local or remote generation of structured creative variables."""

    @property
    def name(self) -> str: ...

    def generate_concept(self, request: ConceptRequest) -> ReelConcept:
        """Return exactly four saves, validated against ReelConcept."""
        ...
