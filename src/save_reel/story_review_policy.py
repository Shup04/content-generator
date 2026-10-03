"""Explicit, offline review-policy upgrade for unfinished world checkpoints."""

from pathlib import Path

from save_reel.models import StageStatus
from save_reel.storage import RunStore
from save_reel.story_models import StorySettings, WorldReviewUpdate
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prompting import StoryPromptCompiler


def prepare_world_recheck(
    run_dir: Path, *, version: str, effort: str = "medium", prompts_dir: Path | None = None,
):
    """Keep approved worlds; review the latest rejected draft before any rewrite.

    Does not call a provider. Ordinary resume keeps its saved policy. This explicit
    upgrade archives the old reviews and grants a fresh, bounded review cycle only
    when the prompt changes. Repeating it cannot replenish an exhausted budget.
    """
    store = RunStore(run_dir)
    with StoryWorkflow._lock(store):
        run = StoryWorkflow.load(run_dir)
        if run.schema_version != "3.0" or run.status not in (
            StageStatus.FAILED, StageStatus.PENDING,
        ):
            raise ValueError("Review-policy changes require a failed or pending v3 story")
        settings = StorySettings.model_validate({
            **run.settings.model_dump(), "world_review_prompt_version": version,
            "world_review_reasoning_effort": effort,
        })
        prompt = StoryPromptCompiler("world_review", version, prompts_dir=prompts_dir).render({
            "CONTEXT": "[CONTEXT]", "FEEDBACK": "[FEEDBACK]",
        })
        if "tiers" in run.templates:
            prompt.text += "\n\n" + run.templates["tiers"].text
        if prompt == run.templates["world_review"]:
            return run
        slots = [s for s in run.saves if not s.final and not (
            s.world_review and s.world_review.approved(run.settings.novelty)
        )]
        if not slots:
            raise ValueError("All worlds already passed review; no recheck is needed")
        run.world_review_updates += (WorldReviewUpdate(
            previous_prompt=run.templates["world_review"],
            previous_effort=run.settings.effort_for("world_review"),
            new_prompt=prompt, new_effort=effort,
            attempts={s.save_id: s.world_attempts for s in slots},
        ),)
        for slot in slots:
            if slot.world is None and slot.world_attempts:
                slot.world = slot.world_attempts[-1].simulation
            slot.world_review = None
            slot.world_attempts = ()
        run.settings = settings
        run.templates["world_review"] = prompt
        run.status, run.error = StageStatus.PENDING, None
        StoryWorkflow._save(run, store)
        from save_reel.story_export import review_text

        store.write_text("story_review.txt", review_text(run), "text/plain")
        return run
