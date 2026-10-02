"""Offline contract fixtures. Useful for testing plumbing, not judging LLM writing quality."""

from itertools import combinations

from save_reel.providers.story import Response
from save_reel.story_models import (
    CandidateRating,
    CandidateReview,
    CandidateSet,
    CartridgeVariables,
    EnvironmentVariables,
    FinalStory,
    NarrationDraft,
    NarrationPolicy,
    SurvivalBrief,
    SurvivalCandidate,
    SurvivalDetails,
    WorldSeed,
)
from save_reel.story_seeds import load_catalog


class MockStoryProvider:
    name = "mock"
    model = "mock-survival-v1"

    def generate(
        self, *, stage: str, prompt: str, context: dict, response_type: type[Response]
    ) -> Response:
        catalog, _ = load_catalog(context["settings"]["seed_catalog_version"])
        seed = WorldSeed.model_validate(context["seed"])
        hazard = next(h for h in catalog["hazards"] if h["danger_type"] == seed.danger_type)
        cost = next(c for c in catalog["costs"] if c["type"] == seed.long_term_cost_type)
        setting = next(
            s for s in catalog["settings"] if s["specific_setting"] == seed.specific_setting
        )
        inhabitants = {
            "alone": "You have never seen another resident.",
            "small_human_group": "Three residents share the ration counter but sleep separately.",
            "nonhuman_workers": "Metal workers maintain the building and ignore spoken requests.",
            "distant_neighbors": (
                "Neighbors signal through their windows but never open their doors."
            ),
        }[seed.social_state]
        if stage == "candidates":
            candidates = []
            for index in range(context["settings"]["candidates_per_save"]):
                variant = catalog["mock_variants"][index % len(catalog["mock_variants"])]
                title = (
                    setting["title"]
                    + ("", " ANNEX", " NIGHT DUTY", " LOWER WORKS", " RESERVE", " LAST SHIFT")[
                        index
                    ]
                )
                candidates.append(
                    SurvivalCandidate(
                        candidate_id=f"{context['save_id']}_candidate_{index + 1}",
                        title=title,
                        premise=(
                            f"Your home is the {seed.specific_setting}. {seed.impossible_property}"
                        ),
                        surface_attraction=f"A warm rest area overlooks {seed.visual_motif}.",
                        resource_situation=variant["food"],
                        safe_sleeping=hazard["sleep"],
                        inhabitants=inhabitants,
                        primary_danger=hazard["threat"],
                        danger_type=seed.danger_type,
                        critical_rule=hazard["rule"],
                        rule_consequence=hazard["consequence"],
                        escape="The service road brings you back to the same entrance.",
                        long_term_drawback=cost["detail"],
                        long_term_cost_type=seed.long_term_cost_type,
                        mundane_detail=variant["routine"],
                        unanswered_mystery=(
                            "Fresh work gloves appear in your size inside a locked drawer."
                        ),
                        visual_hook=f"{seed.visual_motif}. {seed.impossible_property}",
                        severity=seed.severity,
                    )
                )
            result = CandidateSet(candidates=tuple(candidates))
        elif stage == "review":
            result = CandidateReview(
                ratings=tuple(
                    CandidateRating(
                        candidate_id=c["candidate_id"],
                        concrete_daily_life=4,
                        memorable_rule=4,
                        originality=3,
                        visual_potential=4,
                        reason_to_choose=4,
                        reject=False,
                        rationale=(
                            "Offline fixture: concrete supplies, shelter, rule and permanent cost."
                        ),
                    )
                    for c in context["candidates"]["candidates"]
                )
            )
        elif stage == "brief":
            c = SurvivalCandidate.model_validate(context["selected"])
            index = int(c.candidate_id.rsplit("_", 1)[1]) - 1
            variant = catalog["mock_variants"][index % len(catalog["mock_variants"])]
            narration = self._narration(
                seed,
                c,
                variant,
                hazard,
                NarrationPolicy.model_validate(context["narration_policy"]),
            )
            result = FinalStory(
                brief=SurvivalBrief(
                    title=c.title,
                    premise=c.premise,
                    surface_promise=c.surface_attraction,
                    survival=SurvivalDetails(
                        food=c.resource_situation,
                        water=None,
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
                    narration=narration,
                ),
                cartridge=CartridgeVariables(
                    shell_material_color=f"tinted {seed.palette[0]} plastic",
                    shape_language="chunky squared shell with one clipped corner",
                    motifs=seed.visual_motif,
                    object_mood=seed.surface_emotion,
                    hint_scene_description=f"A tight crop of {seed.visual_motif}, "
                    "with the surrounding room hidden in darkness.",
                ),
                environment=EnvironmentVariables(
                    environment_description=f"A {seed.specific_setting} built from "
                    f"{seed.dominant_material}. {c.visual_hook}",
                    time_of_day=seed.time_of_day,
                    key_visual_elements=(seed.visual_motif,),
                    palette=seed.palette,
                    mood=f"{seed.surface_emotion}, {seed.deeper_emotion}",
                    visual_anomaly=seed.impossible_property,
                    environmental_motion="A thin fog plane drifts past the glowing fixture.",
                    shot_composition=f"Eye-level view along the entrance toward "
                    f"{seed.visual_motif}, with a clear foreground path.",
                    key_surfaces=(seed.dominant_material,),
                ),
            )
        elif stage == "narration":
            brief = SurvivalBrief.model_validate(context["brief"])
            # A deterministic rewrite without inventing new lore or mutating the brief.
            lines = list(brief.narration)
            lines[0] = f"You now live here: the {seed.specific_setting}."
            result = NarrationDraft(narration=tuple(lines))
        else:
            raise ValueError("unknown story generation stage")
        return response_type.model_validate(result.model_dump())

    @staticmethod
    def _narration(seed, candidate, variant, hazard, policy):
        facts = (
            variant["narration"],
            candidate.safe_sleeping,
            candidate.long_term_drawback,
            candidate.inhabitants,
        )
        for count in range(policy.min_lines, policy.max_lines + 1):
            for middle in combinations(facts, count - 2):
                lines = (f"Your home is the {seed.specific_setting}.", *middle, hazard["last"])
                try:
                    policy.check(lines)
                except ValueError:
                    continue
                return lines
        raise ValueError(
            "offline fixture cannot fit these narration constraints; "
            "adjust the policy or use the OpenAI provider"
        )
