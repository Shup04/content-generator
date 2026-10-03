"""Adapt cached stories to existing Reel concepts and deterministic visual templates."""

import json

from save_reel.broll_beats import beat_values
from save_reel.media_models import BrollValues, CartridgeValues, MotionValues
from save_reel.models import (
    CartridgeSpec,
    EffectsSpec,
    EnvironmentSpec,
    ReelConcept,
    SaveGameConcept,
    StoryGeneration,
)
from save_reel.pipeline import CreatePipeline
from save_reel.prompting import (
    BrollStillPromptCompiler,
    BrollVideoPromptCompiler,
    CartridgePromptCompiler,
    EnvironmentPromptCompiler,
    LabelPromptCompiler,
)
from save_reel.stages import BrollStillPromptStage, EnvironmentPromptStage, LabelPromptStage
from save_reel.storage import RunStore
from save_reel.story_models import StoryRun, StorySave, WorldConcept


def visual_values(slot: StorySave):
    final = slot.final
    if final is None:
        raise ValueError("cannot export an unfinished story")
    env, cart, brief = final.environment, final.cartridge, final.brief
    cartridge = CartridgeValues(
        title=brief.title,
        shell_material_color=cart.shell_material_color,
        shape_language=cart.shape_language,
        molded_details=cart.motifs,
        object_mood=cart.object_mood,
        hint_scene_description=cart.hint_scene_description,
    )
    broll = BrollValues(
        game_title=brief.title,
        world_scene=f"{env.environment_description}\nTime: {env.time_of_day}.\n"
        f"Key features: {', '.join(env.key_visual_elements)}.\n"
        f"Anomaly: {env.visual_anomaly}",
        colour_palette=", ".join(env.palette),
        mood=env.mood,
        shot_composition=env.shot_composition,
        key_surfaces=", ".join(env.key_surfaces),
    )
    motion = MotionValues(
        camera_motion="A slow wandering walk, gently drifting forward while facing the same "
        "direction, with minimal head movement.",
        environment_motion=env.environmental_motion,
        audio="Silence; audio will be supplied later.",
    )
    return cartridge, broll, motion


class PreparedStoryConceptProvider:
    """Implements the existing ConceptProvider contract from already validated stories."""

    def __init__(self, run: StoryRun):
        self.run = run
        self.name = f"{run.provider}:survival-story"

    def generate_concept(self, request):
        saves = []
        for slot in self.run.saves:
            cart, broll, motion = visual_values(slot)
            env = slot.final.environment
            authored = isinstance(slot.selected, WorldConcept)
            saves.append(
                SaveGameConcept(
                    survivability_tier=slot.final.brief.survivability_tier,
                    title=slot.final.brief.title,
                    summary=slot.final.brief.premise,
                    environment=EnvironmentSpec(
                        biome=slot.seed.setting_family,
                        setting=slot.selected.specific_setting
                        if authored
                        else slot.seed.specific_setting,
                        focal_point=slot.final.brief.visual_hook,
                        time_of_day=env.time_of_day,
                        weather=slot.selected.weather if authored else slot.seed.weather,
                        lighting="simple baked-looking game lighting",
                        palette=env.palette,
                        mood=env.mood,
                        details=env.key_visual_elements,
                        world_scene=broll.world_scene,
                        shot_composition=env.shot_composition,
                        key_surfaces=env.key_surfaces,
                    ),
                    cartridge=CartridgeSpec(
                        shell_color=cart.shell_material_color,
                        material=cart.shell_material_color,
                        label_title=cart.title,
                        label_symbol=cart.hint_scene_description,
                        wear="slightly worn molded edges",
                    ),
                    effects=EffectsSpec(motion_cues=(motion.environment_motion,)),
                )
            )
        return ReelConcept(title="Choose Your Save", saves=tuple(saves))


def review_text(run: StoryRun) -> str:
    lines = [f"CHOOSE YOUR SAVE — {run.run_id}", f"Provider: {run.provider} / {run.model}", ""]
    if run.provider == "mock":
        lines += ["MOCK FIXTURES — not Luna output or a creative-quality evaluation.", ""]
    lines += [f"Status: {run.status.value}", f"Error: {run.error}" if run.error else "", ""]
    for change in run.world_review_updates:
        lines += [f"World review policy: {change.previous_prompt.template.version} -> "
                  f"{change.new_prompt.template.version} ({change.new_effort})"]
        for save_id, attempts in change.attempts.items():
            for i, attempt in enumerate(attempts, 1):
                lines += [f"{save_id} archived review {i}: {attempt.review.model_dump_json()}"]
    if run.reel_review:
        lines += [f"Reel diversity: {run.reel_review.overall:.2f}", run.reel_review.rationale, ""]
    for slot in run.saves:
        if run.schema_version != "1.0":
            lines += _candidate_debug(slot)
        if run.schema_version == "3.0":
            lines += ["WORLD SIMULATION"]
            for replacement in slot.world_replacements:
                lines += [f"Replaced world: {replacement.concept.title}", replacement.reason]
                for i, attempt in enumerate(replacement.attempts, 1):
                    lines += [f"Rejected world review {i}: {attempt.review.model_dump_json()}"]
            if slot.world:
                lines += [json.dumps(slot.world.spec.model_dump(mode="json"), indent=2)]
            for i, attempt in enumerate(slot.world_attempts, 1):
                lines += [f"World review {i}: {attempt.review.model_dump_json()}"]
            if slot.travelogue:
                lines += ["TRAVELOGUE NARRATION — THREE CANDIDATES"]
                if slot.travelogue.description:
                    lines += [f"Writer: {run.settings.narration_model}",
                              "Concise world description:",
                              slot.travelogue.description.description, ""]
                for i, attempt in enumerate(slot.travelogue.attempts, 1):
                    lines += [f"Narration round {i}:"]
                    for candidate in attempt.candidates.candidates:
                        lines += [f"  {candidate.candidate_id}: {candidate.paragraph}"]
                    for rating in attempt.review.ratings:
                        lines += [f"  Rating: {rating.model_dump_json()}"]
                if slot.travelogue.candidates and not slot.travelogue.review:
                    lines += [f"Pending review: {slot.travelogue.candidates.model_dump_json()}"]
                lines += [f"Selected narration: {slot.travelogue.selected_id or 'none'}", ""]
            if not slot.travelogue:
                lines += ["NARRATION GROUNDING"]
            for i, attempt in enumerate(slot.narration_attempts, 1):
                lines += [f"Narration attempt {i}:"]
                for line in attempt.draft.lines:
                    lines += [f"  {line.text} [{', '.join(line.fact_refs)}; {line.tone}]"]
                    lines += [f"  Practical change: {line.practical_change}"]
                lines += [f"Grounding review: {attempt.review.model_dump_json()}"]
        if not slot.final:
            lines += [f"{slot.save_id.upper()} — unfinished", ""]
            continue
        brief, survival = slot.final.brief, slot.final.brief.survival
        lines += [f"SAVE {slot.save_id[-2:]} — {brief.title}", f"Role: {slot.seed.role.value}", ""]
        if brief.survivability_tier:
            lines += [f"Survivability tier: {brief.survivability_tier.value}"]
        for number, beat in enumerate(slot.final.environment.broll_beats or (), 1):
            lines += [f"B-roll {number} ({beat.type}): {beat.description}",
                      f"Framing: {beat.shot_composition}", f"Motion: {beat.camera_motion}", ""]
        for label, value in (
            ("Premise", brief.premise),
            ("Surface attraction", brief.surface_promise),
            ("Food", survival.food),
            ("Water", survival.water),
            ("Shelter", survival.shelter),
            ("Inhabitants", survival.inhabitants),
            ("Threat", survival.main_threat),
            ("Critical rule", survival.critical_rule),
            ("Consequence", survival.rule_consequence),
            ("Long-term cost", survival.long_term_cost),
            ("Escape", survival.escape),
            ("Daily routine", survival.mundane_detail),
            ("Unanswered mystery", brief.unanswered_mystery),
        ):
            if value is not None:
                lines += [f"{label}:", value, ""]
        lines += ["Narration:"]
        lines += list(brief.narration) if slot.travelogue else [
            f"{i}. {line}" for i, line in enumerate(brief.narration, 1)
        ]
        stats = slot.narration_stats
        if stats:
            units = "paragraph" if slot.travelogue else "lines"
            lines += [
                f"{stats.words} words / {stats.lines} {units} / "
                f"~{stats.estimated_seconds}s (estimate only)"
                + ("" if stats.within_target else " — outside target, within tolerance")
            ]
        lines += ["", "-" * 60, ""]
    return "\n".join(lines)


def _candidate_debug(slot):
    lines = [
        f"SAVE {slot.save_id[-2:]} — DEVELOPMENT",
        "Broad seed:",
        json.dumps(slot.seed.model_dump(mode="json"), ensure_ascii=False),
        "",
    ]
    rounds = [
        (a.round_number, a.candidates, a.review, a.assessments) for a in slot.previous_attempts
    ]
    if slot.candidates:
        rounds.append((slot.candidate_round, slot.candidates, slot.review, slot.assessments))
    for number, candidates, review, assessments in rounds:
        lines += [f"Candidate round {number}:"]
        for c in candidates.candidates:
            lines += [
                f"  {c.candidate_id} — {c.title}",
                f"  Setting: {c.specific_setting}",
                f"  Resources: {c.resource_situation}",
                f"  Shelter: {c.safe_sleeping}",
                f"  Inhabitants: {c.inhabitants}",
                f"  Danger: {c.primary_danger}",
                f"  Mechanic: {c.central_mechanic}",
                f"  Rule: {c.critical_rule}",
                f"  Consequence: {c.rule_consequence}",
                f"  Cost: {c.long_term_drawback}",
                f"  Anomaly: {c.anomaly}",
                f"  Visual hook: {c.visual_hook}",
                f"  Palette: {', '.join(c.palette)}",
                f"  Attraction: {c.surface_attraction}",
                f"  Mystery: {c.unanswered_mystery}",
            ]
            lines += [
                f"  Causal {key}: {value}" for key, value in c.causal_logic.model_dump().items()
            ]
            rating = (
                next((r for r in review.ratings if r.candidate_id == c.candidate_id), None)
                if review
                else None
            )
            if rating:
                scores = {k: v for k, v in rating.model_dump().items() if isinstance(v, float)}
                lines += ["  Scores: " + json.dumps(scores), f"  Critic: {rating.rationale}"]
            assessment = assessments.get(c.candidate_id)
            if assessment:
                lines += [
                    f"  Novelty: {assessment.novelty:.2f}",
                    f"  Nearest prior save: {assessment.nearest_title or 'none'}",
                    f"  Similarity: {assessment.similarity:.2f}",
                    "  Decision: " + ("eligible" if assessment.accepted else "rejected"),
                    "  Rejection reasons: " + ("; ".join(assessment.reasons) or "none"),
                ]
            lines += [""]
    lines += [f"Selected candidate: {slot.selected.title if slot.selected else 'none'}", ""]
    return lines


def export_story(run: StoryRun, store: RunStore):
    reel = CreatePipeline(
        PreparedStoryConceptProvider(run),
        stages=(
            EnvironmentPromptStage(EnvironmentPromptCompiler()),
            LabelPromptStage(LabelPromptCompiler()),
            BrollStillPromptStage(BrollStillPromptCompiler("v2")),
        ),
    ).create_in_store(run.request, store)
    for save, slot in zip(reel.saves, run.saves, strict=True):
        cartridge, broll, motion = visual_values(slot)
        prefix = f"saves/{slot.save_id}"
        for number, beat in enumerate(slot.final.environment.broll_beats or (), 1):
            env_values, motion_values = beat_values(broll, motion, beat)
            name = f"broll_{number:02}"
            for kind, values in (("values", env_values), ("motion_values", motion_values),
                                 ("beat", beat)):
                save.artifacts[f"{name}_{kind}"] = store.write_text(
                    f"{prefix}/{name}_{kind}.json", values.model_dump_json(indent=2, by_alias=True),
                    "application/json",
                )
            for kind, compiler, values in (
                ("still", BrollStillPromptCompiler("v2"), env_values.model_dump(by_alias=True)),
                ("video", BrollVideoPromptCompiler("v2"), {
                    **env_values.model_dump(by_alias=True),
                    **motion_values.model_dump(by_alias=True),
                }),
            ):
                prompt = compiler.render(values)
                key = f"{name}_{kind}_prompt"
                save.artifacts[key] = store.write_text(f"{prefix}/{key}.txt", prompt.text)
                save.artifacts[f"{key}_metadata"] = store.write_text(
                    f"{prefix}/{key}.json", prompt.model_dump_json(indent=2), "application/json",
                )
        for name, value in (
            ("story", slot),
            ("survival_brief", slot.final.brief),
            ("cartridge_values", cartridge),
            ("broll_values", broll),
            ("motion_values", motion),
        ):
            save.artifacts[name] = store.write_text(
                f"{prefix}/{name}.json",
                value.model_dump_json(indent=2, by_alias=True, exclude_none=True) + "\n",
                "application/json",
            )
        if slot.world:
            for name, value in (
                ("world_spec", slot.world.spec), ("world_review", slot.world_review),
                ("narration_grounding", slot.narration_draft),
                ("narration_review", slot.grounding_review),
                ("narration_description", slot.travelogue.description if slot.travelogue else None),
                ("narration_candidates", slot.travelogue.candidates if slot.travelogue else None),
                ("narration_selection", slot.travelogue.review if slot.travelogue else None),
                ("narration_travelogue", slot.travelogue),
            ):
                if value is None:
                    continue
                save.artifacts[name] = store.write_text(
                    f"{prefix}/{name}.json", value.model_dump_json(indent=2) + "\n",
                    "application/json",
                )
        cart_prompt = CartridgePromptCompiler("v2").render(
            cartridge.model_dump(by_alias=True, exclude_none=True)
        )
        video_prompt = BrollVideoPromptCompiler("v2").render(
            {
                **broll.model_dump(by_alias=True),
                **motion.model_dump(by_alias=True),
            }
        )
        for name, prompt in (
            ("cartridge_prompt", cart_prompt),
            ("broll_video_prompt", video_prompt),
        ):
            save.artifacts[name] = store.write_text(f"{prefix}/{name}.txt", prompt.text)
            save.artifacts[f"{name}_metadata"] = store.write_text(
                f"{prefix}/{name}.json", prompt.model_dump_json(indent=2), "application/json"
            )
    state_artifact = store.write_text(
        "story.json", run.model_dump_json(indent=2) + "\n", "application/json"
    )
    summary = store.write_text("story_review.txt", review_text(run))
    reel.story_generation = StoryGeneration(
        provider=run.provider,
        model=run.model,
        templates=tuple(p.template for p in run.templates.values()) + (run.seed_catalog,),
        state=state_artifact,
        review=summary,
    )
    store.save_manifest(reel)
    return reel
