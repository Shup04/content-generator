"""Small contracts for the description-to-prose writer; no survival-field quotas."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.models import Model, Text

CandidateId = Literal["narration_1", "narration_2", "narration_3"]


class WorldDescription(Model):
    description: Text

    @model_validator(mode="after")
    def concise(self):
        from save_reel.story_models import word_count

        if word_count((self.description,)) > 180:
            raise ValueError("world description must be at most 180 words")
        return self


class ProseCandidate(Model):
    candidate_id: CandidateId
    paragraph: Text


class ProseParagraph(Model):
    """Only the spoken text; candidate IDs belong to the application."""

    paragraph: Text


class ProseCandidates(Model):
    candidates: Annotated[tuple[ProseCandidate, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def distinct(self):
        if {c.candidate_id for c in self.candidates} != {
            "narration_1", "narration_2", "narration_3"
        }:
            raise ValueError("expected three distinct narration candidate IDs")
        if len({" ".join(c.paragraph.casefold().split()) for c in self.candidates}) != 3:
            raise ValueError("narration candidates must have different paragraphs")
        return self

    def check(self, spec, policy):
        for candidate in self.candidates:
            policy.check((candidate.paragraph,))


class ProseRating(Model):
    candidate_id: CandidateId
    naturalness: float = Field(ge=0, le=1)
    grounded: bool
    issues: tuple[Text, ...]
    rationale: Text


class ProseReview(Model):
    ratings: Annotated[tuple[ProseRating, ...], Field(min_length=3, max_length=3)]

    def check(self, candidates):
        ids = [r.candidate_id for r in self.ratings]
        if len(set(ids)) != 3 or set(ids) != {c.candidate_id for c in candidates.candidates}:
            raise ValueError("narration review must rate every candidate exactly once")

    def choose(self, candidates):
        self.check(candidates)
        eligible = [r for r in self.ratings if r.grounded and not r.issues]
        if not eligible:
            return None
        return max(sorted(eligible, key=lambda r: r.candidate_id),
                   key=lambda r: r.naturalness).candidate_id
