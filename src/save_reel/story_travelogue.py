"""Generate three paragraphs, review independently, and select without changing the world."""

from save_reel.story_narration_models import (
    TravelogueAttempt,
    TravelogueCandidates,
    TravelogueReview,
)
from save_reel.story_prose_models import (
    ProseCandidate,
    ProseCandidates,
    ProseParagraph,
    ProseReview,
    WorldDescription,
)


def validate_travelogue(run, slot):
    state = slot.travelogue
    if slot.narration_draft or slot.grounding_review:
        raise ValueError("travelogue and legacy briefing outputs cannot be mixed in one save")
    if state.drafts:
        if state.policy.prompt_version != "v3" or not state.description:
            raise ValueError("individual drafts require the v3 narration description")
        for i, draft in enumerate(state.drafts, 1):
            if draft.candidate_id != f"narration_{i}":
                raise ValueError("individual narration drafts must be ordered")
            state.policy.check((draft.paragraph,))
    if state.candidates:
        if not slot.world_review or not slot.world_review.approved(run.settings.novelty):
            raise ValueError("travelogue candidates require an approved world")
        state.candidates.check(slot.world.spec, state.policy)
        expected = ProseCandidates if state.policy.prompt_version != "v1" else TravelogueCandidates
        if not isinstance(state.candidates, expected):
            raise ValueError("narration candidates must match their saved prompt version")
        if state.policy.prompt_version != "v1" and state.description is None:
            raise ValueError("Sol narration requires a saved concise world description")
    if state.review:
        if not state.candidates:
            raise ValueError("travelogue review requires saved candidates")
        state.review.check(state.candidates)
        expected = ProseReview if state.policy.prompt_version != "v1" else TravelogueReview
        if not isinstance(state.review, expected):
            raise ValueError("narration review must match its saved prompt version")
    if state.selected_id is not None:
        if not state.review or state.review.choose(state.candidates) != state.selected_id:
            raise ValueError("selected travelogue must be the highest-ranked eligible candidate")
    if slot.final and state.selected is None:
        raise ValueError("final narration requires an approved travelogue selection")


def finish_travelogue(workflow, run, slot, store, logger):
    state = slot.travelogue
    simple = state.policy.prompt_version != "v1"
    if simple and state.description is None:
        state.description = workflow._generate(
            run, slot, store, logger, "narration_description", WorldDescription, lambda value: None,
        )
        workflow._save(run, store)
    while state.selected is None:
        if len(state.attempts) > run.settings.validation_retries:
            raise ValueError(f"{slot.save_id}: travelogue narration approval exhausted")
        if state.candidates is None:
            if simple:
                feedback = {"revision_notes": [
                    issue for rating in state.attempts[-1].review.ratings for issue in rating.issues
                ]} if state.attempts else {}
            else:
                feedback = {
                    "narration_round": len(state.attempts) + 1,
                    "previous_attempt": (state.attempts[-1].model_dump(mode="json")
                                         if state.attempts else None),
                }
            if state.policy.prompt_version == "v3":
                while len(state.drafts) < 3:
                    number = len(state.drafts) + 1

                    def check_paragraph(value):
                        state.policy.check((value.paragraph,))
                        if " ".join(value.paragraph.casefold().split()) in {
                            " ".join(c.paragraph.casefold().split()) for c in state.drafts
                        }:
                            raise ValueError("Write a distinct narration, not the previous wording")

                    paragraph = workflow._generate(
                        run, slot, store, logger, "narration_prose_candidate", ProseParagraph,
                        check_paragraph, candidate_number=number,
                        narration_round=len(state.attempts) + 1, **feedback,
                    )
                    state.drafts += (ProseCandidate(
                        candidate_id=f"narration_{number}", paragraph=paragraph.paragraph,
                    ),)
                    workflow._save(run, store)
                state.candidates = ProseCandidates(candidates=state.drafts)
            else:
                state.candidates = workflow._generate(
                    run, slot, store, logger,
                    "narration_prose_candidates" if simple else "narration_candidates",
                    ProseCandidates if simple else TravelogueCandidates,
                    lambda value: value.check(slot.world.spec, state.policy),
                    **feedback,
                )
            workflow._save(run, store)
        state.review = workflow._generate(
            run, slot, store, logger,
            "narration_prose_selection" if simple else "narration_selection",
            ProseReview if simple else TravelogueReview,
            lambda value: value.check(state.candidates),
            candidates=state.candidates.model_dump(mode="json"),
        )
        state.attempts += (TravelogueAttempt(candidates=state.candidates, review=state.review),)
        state.selected_id = state.review.choose(state.candidates)
        if state.selected_id is None:
            logger.warning("%s: all three travelogues rejected: %s", slot.save_id,
                           state.review.model_dump_json())
            state.candidates = None
            state.review = None
            state.drafts = ()
        workflow._save(run, store)
