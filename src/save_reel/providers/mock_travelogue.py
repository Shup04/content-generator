"""Offline contract fixtures only; these scores are not a literary evaluation."""

from save_reel.story_narration_models import (
    NarrationFact,
    TravelogueCandidate,
    TravelogueCandidates,
    TravelogueRating,
    TravelogueReview,
)


def generate_travelogue_fixture(stage, context, response_type):
    if stage == "narration_candidates":
        facts = context["approved_facts"]
        environment, food, shelter, rule = (
            facts[key] for key in ("environment", "food", "shelter", "critical_survival_rule")
        )
        lower = lambda s: s[:1].lower() + s[1:]  # noqa: E731
        transitions = (
            "That becomes part of your everyday routine, while you remember to",
            "As you get used to living here, you remember to",
            "While you settle into that everyday routine, you remember to",
        )
        result = TravelogueCandidates(candidates=tuple(
            TravelogueCandidate(
                candidate_id=f"narration_{i}",
                paragraph=f"{environment} {food.rstrip('.')}, and {lower(shelter)} "
                          f"{transition} {lower(rule)}",
                facts=tuple(NarrationFact(source=key, quote=facts[key]) for key in (
                    "environment", "food", "shelter", "critical_survival_rule"
                )),
                sensory_details=(environment,), ordinary_activity=lower(shelter),
                main_danger_or_rule=rule,
            ) for i, transition in enumerate(transitions, 1)
        ))
    elif stage == "narration_selection":
        candidates = TravelogueCandidates.model_validate(context["candidates"])
        result = TravelogueReview(ratings=tuple(
            TravelogueRating(
                candidate_id=c.candidate_id, grounded=True, second_person=True,
                connected_prose=True, checklist_like=False, sensory_detail_count=1,
                ordinary_activity_present=True, distinct_fact_count=len(c.facts),
                main_danger_count=1, creepy_asides_or_poetry=False, forced_twist=False,
                spoken_flow=0.8 + i * 0.05, not_list_like=0.8, cliche_free=0.8,
                issues=(), rationale="Offline fixture rating, not a semantic or style judgment.",
            ) for i, c in enumerate(candidates.candidates)
        ))
    else:
        raise ValueError("unknown travelogue fixture stage")
    return response_type.model_validate(result.model_dump())
