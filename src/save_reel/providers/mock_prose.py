"""Offline prose contract fixtures, not a creative-quality demonstration."""

from save_reel.story_prose_models import (
    ProseCandidate,
    ProseCandidates,
    ProseParagraph,
    ProseRating,
    ProseReview,
    WorldDescription,
)


def generate_prose_fixture(stage, context, response_type):
    if stage == "narration_description":
        facts = context["approved_facts"]
        result = WorldDescription(description=" ".join(
            facts[key] for key in ("environment", "daily_routine", "critical_survival_rule")
        ))
    elif stage == "narration_prose_candidate":
        opening = ("Most days", "Each day", "After a while")[context["candidate_number"] - 1]
        result = ProseParagraph(paragraph=(
            f"{opening}, you get used to this place. You spend some time on small jobs "
            "and some time taking a break. It helps to know where to go and when to "
            "come back. You follow the rule you were told, then get on with your day. "
            "There is no need to rush through every task."
        ))
    elif stage == "narration_prose_candidates":
        # Deliberately generic fixture, never advertised as generated literary quality.
        result = ProseCandidates(candidates=tuple(
            ProseCandidate(candidate_id=f"narration_{i}", paragraph=(
                f"{opening} you get used to the place while going about your usual routine. "
                "There is time to notice your surroundings as you move between one task "
                "and the next, and you begin to know the route. The description gives "
                "you a sense of everyday life here, with ordinary things to do between "
                "the moments when you need to be careful."
            )) for i, opening in enumerate(("At first,", "Over time,", "Most days,"), 1)
        ))
    else:
        candidates = ProseCandidates.model_validate(context["candidates"])
        result = ProseReview(ratings=tuple(
            ProseRating(candidate_id=c.candidate_id, naturalness=0.8 + i * 0.05,
                        grounded=True, issues=(), rationale="Offline fixture rating only.")
            for i, c in enumerate(candidates.candidates)
        ))
    return response_type.model_validate(result.model_dump())
