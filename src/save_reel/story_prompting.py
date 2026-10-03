"""Story prompts use the same strict compiler and version references as image prompts."""

import json
import re
from pathlib import Path

from save_reel.models import CompiledPrompt
from save_reel.prompting import PromptCompiler

STORY_STAGES = ("candidates", "review", "brief", "narration", "reel_review")
SIMULATION_STAGES = (
    "candidates", "review", "reel_review", "world_simulation", "world_review",
    "narration", "narration_review",
)
TRAVELOGUE_STAGES = ("narration_candidates", "narration_selection")
PROSE_STAGES = ("narration_description", "narration_prose_candidates", "narration_prose_selection")
PLAIN_STAGES = ("narration_description", "narration_prose_candidate", "narration_prose_selection")


class StoryPromptCompiler(PromptCompiler):
    bracket_placeholders = True

    def __init__(self, stage: str, version: str = "v1", *, prompts_dir: Path | None = None):
        if stage not in (
            *STORY_STAGES, *SIMULATION_STAGES, *TRAVELOGUE_STAGES, *PROSE_STAGES, *PLAIN_STAGES,
            "narration_examples", "rules", "tiers", "broll_beats",
        ):
            raise ValueError("unknown story prompt stage")
        self.template_name = f"story_{stage}"
        super().__init__(version, prompts_dir=prompts_dir)

    def _variables(self, request, save):
        raise ValueError("story prompts require explicit structured context")


def load_prompts(version: str, prompts_dir: Path | None = None,
                 tier_version: str | None = None,
                 broll_version: str | None = None,
                 world_review_version: str | None = None) -> dict[str, CompiledPrompt]:
    rules = StoryPromptCompiler("rules", version, prompts_dir=prompts_dir).render({})
    # Keep unresolved context slots in the snapshot so resume never reads revised templates.
    result = {"rules": rules}
    if tier_version and version != "v1":
        tiers = StoryPromptCompiler("tiers", tier_version, prompts_dir=prompts_dir).render({})
        result["tiers"] = tiers
        rules = rules.model_copy(update={"text": rules.text + "\n\n" + tiers.text})
    stages = (
        STORY_STAGES[:-1] if version == "v1"
        else STORY_STAGES if version == "v2" else SIMULATION_STAGES
    )
    for stage in stages:
        stage_version = world_review_version if stage == "world_review" else None
        result[stage] = StoryPromptCompiler(
            stage, stage_version or version, prompts_dir=prompts_dir
        ).render(
            {
                "WRITING_RULES": rules.text,
                "CONTEXT": "[CONTEXT]",
                "FEEDBACK": "[FEEDBACK]",
                "HISTORY": "[HISTORY]",
            }
        )
        if "tiers" in result and stage in ("world_simulation", "world_review"):
            result[stage].text += "\n\n" + result["tiers"].text
    if broll_version and version != "v1":
        beats = StoryPromptCompiler(
            "broll_beats", broll_version, prompts_dir=prompts_dir
        ).render({})
        result["broll_beats"] = beats
        stage = "brief" if version == "v2" else "world_simulation"
        result[stage].text += "\n\n" + beats.text
    return result


def load_travelogue_prompts(version="v1", prompts_dir=None) -> dict[str, CompiledPrompt]:
    # Narration has its own version; changing style must not change the world prompts.
    examples = (
        StoryPromptCompiler("narration_examples", version, prompts_dir=prompts_dir).render({}).text
        if version == "v2" else ""
    )
    return {
        stage: StoryPromptCompiler(stage, version, prompts_dir=prompts_dir).render(
            {"CONTEXT": "[CONTEXT]", "FEEDBACK": "[FEEDBACK]", "EXAMPLES": examples}
        )
        for stage in (PLAIN_STAGES if version == "v3"
                      else PROSE_STAGES if version == "v2" else TRAVELOGUE_STAGES)
    }


def render_request(prompt: CompiledPrompt, context: dict, feedback: str = "") -> str:
    # Substitute only template slots, not tokens appearing inside user/model-provided values.
    body = dict(context)
    history = body.pop("creative_history", None)
    values = {
        "CONTEXT": json.dumps(body, ensure_ascii=False, indent=2),
        "HISTORY": json.dumps(history or {}, ensure_ascii=False, indent=2),
        "FEEDBACK": feedback,
    }
    return re.sub(r"\[(CONTEXT|HISTORY|FEEDBACK)\]", lambda m: values[m[1]], prompt.text)
