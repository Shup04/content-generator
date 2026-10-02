"""Finite offline stage fixtures, not a semantic evaluator or creative model."""

import json
from importlib.resources import files

from save_reel.story_models import (
    GroundedLine,
    GroundedNarration,
    GroundingReview,
    NarrationLineReview,
    WorldApproval,
    WorldConcept,
    WorldSimulation,
    WorldSpec,
)


def generate_simulation_fixture(stage, context, response_type):
    if stage == "world_simulation":
        from save_reel.providers.mock_story import MockStoryProvider

        c = WorldConcept.model_validate(context["selected"])
        from save_reel.story_models import FinalStory

        legacy = MockStoryProvider("v2").generate(
            stage="brief", prompt="", context=context, response_type=FinalStory
        )
        result = WorldSimulation(
            spec=WorldSpec(
                environment=c.premise, resource_system=c.resource_situation,
                food=c.resource_situation, water=None, shelter=c.safe_sleeping,
                inhabitants=c.inhabitants, threat=c.primary_danger, warning_signs=(),
                critical_survival_rule=c.critical_rule, consequence_if_broken=c.rule_consequence,
                escape_conditions=c.escape, daily_routine=c.mundane_detail,
                long_term_cost=c.long_term_drawback, causal_logic=c.causal_logic,
            ),
            cartridge=legacy.cartridge, environment=legacy.environment,
        )
    elif stage == "world_review":
        result = WorldApproval(
            causal_coherence=0.9, preserves_selected_concept=True, practical_system=True, issues=()
        )
    elif stage == "narration":
        fixtures = json.loads(
            files("save_reel.prompt_templates").joinpath("story_fixtures", "v2.json").read_text()
        )
        fixture = next(f for f in fixtures if f["concept"]["title"] == context["title"])
        refs = ("environment", "food", "shelter", "critical_survival_rule")
        result = GroundedNarration(
            lines=tuple(
                GroundedLine(
                    text=text, fact_refs=(ref,),
                    tone="unsettling" if index == 3 else "practical",
                    practical_change=f"Offline fixture: explains {ref}.",
                )
                for index, (text, ref) in enumerate(zip(fixture["narration"], refs, strict=True))
            )
        )
    elif stage == "narration_review":
        draft = GroundedNarration.model_validate(context["draft"])
        result = GroundingReview(
            lines=tuple(
                NarrationLineReview(
                    line_number=i, supported_by_facts=True, changes_practical_understanding=True,
                    repeats_information=False, tone=line.tone, issue=None,
                )
                for i, line in enumerate(draft.lines, 1)
            )
        )
    else:
        raise ValueError("unknown simulation fixture stage")
    return response_type.model_validate(result.model_dump())
