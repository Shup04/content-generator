"""Append grounded shot descriptions without rewriting a saved world or its media."""

import hashlib
import json

from pydantic import Field

from save_reel.broll_beats import BrollBeat, StudioBrollBeat, validate_beats
from save_reel.models import Model
from save_reel.story_prompting import StoryPromptCompiler, render_request
from save_reel.studio_store import write_json


class AdditionalViews(Model):
    beats: tuple[BrollBeat, ...] = Field(min_length=1, max_length=3)


def plan_views(game, count, *, settings, prompts, folder, provider, world=None):
    """Checkpoint the response before applying it, so a resume reuses the same request."""
    existing = game.broll_beats or (StudioBrollBeat(
        type="establishing", description=game.environment.world_scene,
        shot_composition=game.environment.shot_composition,
        camera_motion=game.motion.camera_motion, run_id=game.broll_run,
    ),)
    if len(existing) >= count:
        return
    context = {
        "title": game.title, "approved_world": world,
        "environment": game.environment.model_dump(), "narration": game.narration,
        "existing_views": [BrollBeat.model_validate(b.model_dump(exclude={"run_id"}))
                           .model_dump() for b in existing],
        "additional_view_count": count - len(existing),
    }
    template = StoryPromptCompiler("broll_expansion", "v1", prompts_dir=prompts).render(
        {"CONTEXT": "[CONTEXT]", "FEEDBACK": "[FEEDBACK]"}
    )
    prompt = render_request(template, context)
    digest = hashlib.sha256(json.dumps(
        {"prompt": prompt, "settings": settings.model_dump()}, sort_keys=True,
    ).encode()).hexdigest()
    checkpoint = folder / f"views-{digest}.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if "response" not in saved:
            raise ValueError("Additional-view request has no saved response. Check the job "
                             "before starting a new generation; it will not retry automatically.")
        response = AdditionalViews.model_validate(saved["response"])
    else:
        write_json(checkpoint, {"status": "requested", "context": context})
        response = provider.generate(stage="broll_expansion", prompt=prompt, context=context,
                                     response_type=AdditionalViews)
        write_json(checkpoint, {"status": "received", "context": context,
                                "response": response.model_dump(mode="json")})
    if len(response.beats) != context["additional_view_count"]:
        raise ValueError("Additional-view planner returned the wrong number of shots")
    combined = (*existing, *(StudioBrollBeat(**b.model_dump()) for b in response.beats))
    validate_beats(combined)
    game.broll_beats = combined

