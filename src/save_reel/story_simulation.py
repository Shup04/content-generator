"""V3: approve a factual world before writing and checking its spoken briefing."""

from save_reel.story_models import (
    FinalStory,
    GroundedNarration,
    GroundingReview,
    NarrationAttempt,
    NarrationStats,
    SurvivalBrief,
    SurvivalDetails,
    WorldApproval,
    WorldAttempt,
    WorldSimulation,
)


def check_world(slot, world: WorldSimulation) -> None:
    if slot.selected is None:
        raise ValueError("simulation requires a selected concept")
    c, spec = slot.selected, world.spec
    for field, expected in (
        ("resource_system", c.resource_situation),
        ("shelter", c.safe_sleeping),
        ("inhabitants", c.inhabitants),
        ("threat", c.primary_danger),
        ("critical_survival_rule", c.critical_rule),
        ("consequence_if_broken", c.rule_consequence),
        ("escape_conditions", c.escape),
        ("long_term_cost", c.long_term_drawback),
    ):
        if getattr(spec, field) != expected:
            raise ValueError(f"world.{field} must preserve the selected concept exactly")
    if (
        world.environment.palette != c.palette
        or world.environment.visual_anomaly != c.anomaly
        or world.environment.time_of_day != c.time_of_day
    ):
        raise ValueError("simulation visuals must preserve selected palette, anomaly and time")


def assemble(slot) -> FinalStory:
    """Deterministic bridge to the existing manifest and visual-generation contracts."""
    c, world, draft = slot.selected, slot.world, slot.narration_draft
    spec = world.spec
    return FinalStory(
        brief=SurvivalBrief(
            title=c.title,
            premise=c.premise,
            surface_promise=c.surface_attraction,
            survival=SurvivalDetails(
                food=spec.food, water=spec.water, shelter=spec.shelter,
                inhabitants=spec.inhabitants, main_threat=spec.threat,
                critical_rule=spec.critical_survival_rule,
                rule_consequence=spec.consequence_if_broken,
                escape=spec.escape_conditions, mundane_detail=spec.daily_routine,
                long_term_cost=spec.long_term_cost,
            ),
            visual_hook=c.visual_hook,
            unanswered_mystery=c.unanswered_mystery,
            severity=c.severity,
            narration=(slot.travelogue.selected.paragraph,) if slot.travelogue else draft.narration,
        ),
        cartridge=world.cartridge,
        environment=world.environment,
    )


def validate_slot(run, slot) -> None:
    if slot.world:
        check_world(slot, slot.world)
    if slot.world_review and not slot.world:
        raise ValueError("world approval requires a saved simulation")
    if slot.travelogue:
        from save_reel.story_travelogue import validate_travelogue

        validate_travelogue(run, slot)
        if slot.final and slot.final != assemble(slot):
            raise ValueError("final story must match its approved world and selected travelogue")
        return
    if slot.narration_draft:
        if not slot.world_review or not slot.world_review.approved(run.settings.novelty):
            raise ValueError("narration requires an approved world simulation")
        slot.narration_draft.check(slot.world.spec, run.settings.narration)
    if slot.grounding_review:
        if not slot.narration_draft:
            raise ValueError("grounding review requires saved narration")
        slot.grounding_review.check(slot.narration_draft)
    if slot.final:
        if not slot.grounding_review or slot.grounding_review.problems(slot.narration_draft):
            raise ValueError("final narration requires a passing grounding and usefulness review")
        if slot.final != assemble(slot):
            raise ValueError("final story must match its approved world and grounded narration")


def finish_worlds(workflow, run, store, logger):
    """Quality rejection rewrites the output, never retries a critic to force approval."""
    for slot in run.saves:
        if slot.final is not None:
            continue
        while slot.world_review is None or not slot.world_review.approved(run.settings.novelty):
            if len(slot.world_attempts) > run.settings.validation_retries:
                raise ValueError(f"{slot.save_id}: world simulation approval exhausted")
            if slot.world is None:
                slot.world = workflow._generate(
                    run, slot, store, logger, "world_simulation", WorldSimulation,
                    lambda value: check_world(slot, value),
                    simulation_round=len(slot.world_attempts) + 1,
                    previous_review=(
                        slot.world_attempts[-1].review.model_dump(mode="json")
                        if slot.world_attempts else None
                    ),
                    previous_simulation=(
                        slot.world_attempts[-1].simulation.model_dump(mode="json")
                        if slot.world_attempts else None
                    ),
                    earlier_review_issues=tuple(dict.fromkeys(
                        issue for attempt in slot.world_attempts[:-1]
                        for issue in attempt.review.issues
                    )),
                )
                workflow._save(run, store)
            slot.world_review = workflow._generate(
                run, slot, store, logger, "world_review", WorldApproval, lambda value: None,
                simulation=slot.world.model_dump(mode="json"),
            )
            slot.world_attempts += (WorldAttempt(simulation=slot.world, review=slot.world_review),)
            if not slot.world_review.approved(run.settings.novelty):
                logger.warning("%s world rejected: %s", slot.save_id, slot.world_review.issues)
                slot.world = None
                slot.world_review = None
            workflow._save(run, store)

        if slot.travelogue:
            from save_reel.story_travelogue import finish_travelogue

            finish_travelogue(workflow, run, slot, store, logger)
            slot.final = assemble(slot)
            slot.narration_stats = NarrationStats.measure(
                slot.final.brief.narration, slot.travelogue.policy
            )
            workflow._save(run, store)
            continue

        while slot.grounding_review is None or slot.grounding_review.problems(slot.narration_draft):
            if len(slot.narration_attempts) > run.settings.validation_retries:
                raise ValueError(f"{slot.save_id}: narration grounding approval exhausted")
            if slot.narration_draft is None:
                slot.narration_draft = workflow._generate(
                    run, slot, store, logger, "narration", GroundedNarration,
                    lambda value: value.check(slot.world.spec, run.settings.narration),
                    narration_round=len(slot.narration_attempts) + 1,
                    previous_attempt=(
                        slot.narration_attempts[-1].model_dump(mode="json")
                        if slot.narration_attempts else None
                    ),
                )
                workflow._save(run, store)
            slot.grounding_review = workflow._generate(
                run, slot, store, logger, "narration_review", GroundingReview,
                lambda value: value.check(slot.narration_draft),
                draft=slot.narration_draft.model_dump(mode="json"),
            )
            slot.narration_attempts += (
                NarrationAttempt(draft=slot.narration_draft, review=slot.grounding_review),
            )
            problems = slot.grounding_review.problems(slot.narration_draft)
            if problems:
                logger.warning("%s narration rejected: %s", slot.save_id, "; ".join(problems))
                slot.narration_draft = None
                slot.grounding_review = None
            workflow._save(run, store)
        slot.final = assemble(slot)
        slot.narration_stats = NarrationStats.measure(
            slot.final.brief.narration, run.settings.narration
        )
        workflow._save(run, store)
