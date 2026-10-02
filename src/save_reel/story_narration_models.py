"""Travelogue narration contracts, independent of world invention and media timing."""

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.models import Model, Text
from save_reel.story_prose_models import (
    ProseCandidate,
    ProseCandidates,
    ProseReview,
    WorldDescription,
)

CandidateId = Literal["narration_1", "narration_2", "narration_3"]
Score = Annotated[float, Field(ge=0, le=1)]


class TraveloguePolicy(Model):
    prompt_version: Literal["v1", "v2", "v3"] = "v1"
    target_min_words: int = Field(default=40, ge=20)
    target_max_words: int = Field(default=60, ge=20)
    estimated_words_per_minute: int = Field(default=195, ge=60, le=300)

    @classmethod
    def sol(cls):
        return cls(prompt_version="v3", target_min_words=45, target_max_words=70)

    @model_validator(mode="after")
    def valid_range(self):
        if self.target_min_words > self.target_max_words:
            raise ValueError("narration minimum must not exceed maximum")
        return self

    def check(self, paragraphs: tuple[str, ...]) -> None:
        from save_reel.story_models import word_count

        if len(paragraphs) != 1 or any(c in paragraphs[0] for c in "\n\r"):
            raise ValueError("travelogue must be one connected paragraph")
        count = word_count(paragraphs)
        if not self.target_min_words <= count <= self.target_max_words:
            raise ValueError(
                f"travelogue has {count} words; needs "
                f"{self.target_min_words}–{self.target_max_words}"
            )
        if not re.search(r"\byou(?:r|rs|['’](?:ll|re|ve|d))?\b", paragraphs[0], re.I):
            raise ValueError("travelogue must address the viewer in second person")


class NarrationFact(Model):
    source: Text
    quote: Text


class TravelogueCandidate(Model):
    candidate_id: CandidateId
    paragraph: Text
    # Metadata is never spoken. The reviewer checks actual claims independently.
    facts: Annotated[tuple[NarrationFact, ...], Field(min_length=3, max_length=4)]
    sensory_details: Annotated[tuple[Text, ...], Field(min_length=1, max_length=2)]
    ordinary_activity: Text
    main_danger_or_rule: Text | None

    def check(self, spec, policy: TraveloguePolicy) -> None:
        policy.check((self.paragraph,))
        facts = spec.facts()
        seen = set()
        for fact in self.facts:
            if fact.source not in facts or fact.quote not in facts[fact.source]:
                raise ValueError("narration fact must quote an available approved world fact")
            identity = (fact.source, fact.quote.casefold())
            if identity in seen:
                raise ValueError("narration facts must be distinct")
            seen.add(identity)
        for excerpt in (*self.sensory_details, self.ordinary_activity):
            if excerpt not in self.paragraph:
                raise ValueError("sensory details and ordinary activity must quote the paragraph")


class TravelogueCandidates(Model):
    candidates: Annotated[tuple[TravelogueCandidate, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def distinct(self):
        if {c.candidate_id for c in self.candidates} != {
            "narration_1", "narration_2", "narration_3"
        }:
            raise ValueError("expected three distinct narration candidate IDs")
        texts = {" ".join(c.paragraph.casefold().split()) for c in self.candidates}
        if len(texts) != 3:
            raise ValueError("narration candidates must have different paragraphs")
        return self

    def check(self, spec, policy: TraveloguePolicy) -> None:
        for candidate in self.candidates:
            candidate.check(spec, policy)


class TravelogueRating(Model):
    candidate_id: CandidateId
    grounded: bool
    second_person: bool
    connected_prose: bool
    checklist_like: bool
    sensory_detail_count: int = Field(ge=0)
    ordinary_activity_present: bool
    distinct_fact_count: int = Field(ge=0)
    main_danger_count: int = Field(ge=0)
    creepy_asides_or_poetry: bool
    forced_twist: bool
    spoken_flow: Score
    not_list_like: Score
    cliche_free: Score
    issues: tuple[Text, ...]
    rationale: Text

    @property
    def eligible(self) -> bool:
        return (
            self.grounded and self.second_person and self.connected_prose
            and not self.checklist_like and 1 <= self.sensory_detail_count <= 2
            and self.ordinary_activity_present and 3 <= self.distinct_fact_count <= 4
            and self.main_danger_count <= 1 and not self.creepy_asides_or_poetry
            and not self.forced_twist and not self.issues
        )


class TravelogueReview(Model):
    ratings: Annotated[tuple[TravelogueRating, ...], Field(min_length=3, max_length=3)]

    def check(self, candidates: TravelogueCandidates) -> None:
        ids = [r.candidate_id for r in self.ratings]
        if len(set(ids)) != 3 or set(ids) != {c.candidate_id for c in candidates.candidates}:
            raise ValueError("narration review must rate every candidate exactly once")

    def choose(self, candidates: TravelogueCandidates) -> CandidateId | None:
        self.check(candidates)
        eligible = [r for r in self.ratings if r.eligible]
        if not eligible:
            return None
        # The requested priority order; stable ID order breaks exact ties.
        winner = max(
            sorted(eligible, key=lambda r: r.candidate_id),
            key=lambda r: (
                r.spoken_flow, r.not_list_like, r.cliche_free, -r.distinct_fact_count
            ),
        )
        return winner.candidate_id


class TravelogueAttempt(Model):
    candidates: TravelogueCandidates | ProseCandidates
    review: TravelogueReview | ProseReview


class TravelogueState(Model):
    policy: TraveloguePolicy = Field(default_factory=TraveloguePolicy)
    description: WorldDescription | None = None
    drafts: Annotated[tuple[ProseCandidate, ...], Field(max_length=3)] = ()
    candidates: TravelogueCandidates | ProseCandidates | None = None
    review: TravelogueReview | ProseReview | None = None
    selected_id: CandidateId | None = None
    attempts: tuple[TravelogueAttempt, ...] = ()

    @property
    def selected(self) -> TravelogueCandidate | ProseCandidate | None:
        if self.candidates:
            return next(
                (c for c in self.candidates.candidates if c.candidate_id == self.selected_id), None
            )
        return None
