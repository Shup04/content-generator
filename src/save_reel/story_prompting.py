"""Story prompts use the same strict compiler and version references as image prompts."""

import json
import re
from pathlib import Path

from save_reel.models import CompiledPrompt
from save_reel.prompting import PromptCompiler

STORY_STAGES = ("candidates", "review", "brief", "narration", "reel_review")


class StoryPromptCompiler(PromptCompiler):
    bracket_placeholders = True

    def __init__(self, stage: str, version: str = "v1", *, prompts_dir: Path | None = None):
        if stage not in (*STORY_STAGES, "rules"):
            raise ValueError("unknown story prompt stage")
        self.template_name = f"story_{stage}"
        super().__init__(version, prompts_dir=prompts_dir)

    def _variables(self, request, save):
        raise ValueError("story prompts require explicit structured context")


def load_prompts(version: str, prompts_dir: Path | None = None) -> dict[str, CompiledPrompt]:
    rules = StoryPromptCompiler("rules", version, prompts_dir=prompts_dir).render({})
    # Keep unresolved context slots in the snapshot so resume never reads revised templates.
    result = {"rules": rules}
    for stage in STORY_STAGES[:-1] if version == "v1" else STORY_STAGES:
        result[stage] = StoryPromptCompiler(stage, version, prompts_dir=prompts_dir).render(
            {
                "WRITING_RULES": rules.text,
                "CONTEXT": "[CONTEXT]",
                "FEEDBACK": "[FEEDBACK]",
                "HISTORY": "[HISTORY]",
            }
        )
    return result


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
