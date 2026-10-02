"""Whole-world offline fixtures. This adapter does not simulate Luna's creative quality."""

import json
from importlib.resources import files

from save_reel.providers.mock_story_v1 import MockStoryProvider as LegacyMockStoryProvider
from save_reel.story_models import (
    CartridgeVariables,
    EnvironmentVariables,
    FinalStory,
    NarrationDraft,
    ReelDiversityReview,
    SurvivalBrief,
    SurvivalDetails,
    WorldCandidateRating,
    WorldCandidateReview,
    WorldCandidateSet,
    WorldConcept,
)


class MockStoryProvider:
    name = "mock"

    def __init__(self, version="v2"):
        self.version = version
        self.model = f"mock-survival-{version}"

    def generate(self, *, stage, prompt, context, response_type):
        if self.version == "v1":
            return LegacyMockStoryProvider().generate(
                stage=stage, prompt=prompt, context=context, response_type=response_type
            )
        fixtures = json.loads(
            files("save_reel.prompt_templates")
            .joinpath("story_fixtures", "v2.json")
            .read_text(encoding="utf-8")
        )
        if stage == "candidates":
            index = int(context["save_id"][-2:]) - 1
            choices = fixtures[index * 3 : (index + 1) * 3]
            if context["settings"]["candidates_per_save"] != 3:
                raise ValueError("v2 mock fixtures support exactly three candidates per save")
            candidates = []
            for offset, fixture in enumerate(choices, 1):
                data = dict(fixture["concept"])
                data["candidate_id"] = f"{context['save_id']}_candidate_{offset}"
                candidates.append(WorldConcept.model_validate(data))
            result = WorldCandidateSet(candidates=tuple(candidates))
        elif stage == "review":
            result = WorldCandidateReview(
                ratings=tuple(
                    WorldCandidateRating(
                        candidate_id=c["candidate_id"],
                        novelty=0.9,
                        causal_coherence=0.9,
                        survival_specificity=0.9,
                        visual_hook=0.9,
                        choice_appeal=0.9,
                        mystery=0.9,
                        non_poetic_writing=0.9,
                        overall=0.9,
                        nearest_history_id=None,
                        history_similarity=0,
                        rejection_reason=None,
                        rationale=(
                            "Offline fixture score, not an actual creative-quality evaluation."
                        ),
                    )
                    for c in context["candidates"]["candidates"]
                )
            )
        elif stage == "reel_review":
            result = ReelDiversityReview(
                environment=0.9,
                resource=0.9,
                threat=0.9,
                rule=0.9,
                inhabitants=0.9,
                cost=0.9,
                emotion=0.9,
                palette=0.9,
                overall=0.9,
                issues=(),
                weakest_save_id=None,
                rationale="Offline fixture assessment; deterministic filters still apply.",
            )
        elif stage == "brief":
            c = WorldConcept.model_validate(context["selected"])
            fixture = next(f for f in fixtures if f["concept"]["title"] == c.title)
            result = FinalStory(
                brief=SurvivalBrief(
                    title=c.title,
                    premise=c.premise,
                    surface_promise=c.surface_attraction,
                    survival=SurvivalDetails(
                        food=c.resource_situation,
                        shelter=c.safe_sleeping,
                        inhabitants=c.inhabitants,
                        main_threat=c.primary_danger,
                        critical_rule=c.critical_rule,
                        rule_consequence=c.rule_consequence,
                        escape=c.escape,
                        mundane_detail=c.mundane_detail,
                        long_term_cost=c.long_term_drawback,
                    ),
                    visual_hook=c.visual_hook,
                    unanswered_mystery=c.unanswered_mystery,
                    severity=c.severity,
                    narration=tuple(fixture["narration"]),
                ),
                cartridge=CartridgeVariables(
                    shell_material_color=f"tinted {c.palette[0]} plastic",
                    shape_language="chunky rectangular shell",
                    motifs=c.visual_hook,
                    object_mood="uneasy curiosity",
                    hint_scene_description=c.visual_hook,
                ),
                environment=EnvironmentVariables(
                    environment_description=c.architecture_style,
                    time_of_day=c.time_of_day,
                    key_visual_elements=(c.visual_hook,),
                    palette=c.palette,
                    mood="lonely, uneasy",
                    visual_anomaly=c.anomaly,
                    environmental_motion="A faint glow moves over the nearby surfaces.",
                    shot_composition=f"Eye-level view toward {c.visual_hook}.",
                    key_surfaces=(c.dominant_material,),
                ),
            )
        elif stage == "narration":
            lines = list(context["brief"]["narration"])
            lines[0] = "You arrive here permanently."
            result = NarrationDraft(narration=tuple(lines))
        else:
            raise ValueError("unknown mock stage")
        return response_type.model_validate(result.model_dump())
