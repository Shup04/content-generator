"""Stages enrich a reel and persist artifacts without knowing the CLI/provider."""

import logging
from typing import Protocol

from save_reel.models import Reel, StageState, StageStatus, utc_now
from save_reel.prompting import PromptCompiler
from save_reel.storage import RunStore


class ReelStage(Protocol):
    @property
    def name(self) -> str: ...

    def run(self, reel: Reel, store: RunStore, logger: logging.Logger) -> None:
        """Update reel state and record artifacts; raise on failure."""
        ...


class PromptStage:
    name: str
    prompt_field: str

    def __init__(self, compiler: PromptCompiler) -> None:
        self.compiler = compiler

    def run(self, reel: Reel, store: RunStore, logger: logging.Logger) -> None:
        for save in reel.saves:
            save.stages[self.name] = StageState()
        for save in reel.saves:
            state = StageState(status=StageStatus.RUNNING, started_at=utc_now())
            save.stages[self.name] = state
            try:
                prompt = self.compiler.compile(reel.request, save)
                artifact = store.write_text(
                    f"saves/{save.save_id}/{self.prompt_field}.txt", prompt.text
                )
                setattr(save, self.prompt_field, prompt)
                save.artifacts[self.prompt_field] = artifact
                state.status = StageStatus.COMPLETED
                logger.info("Compiled %s for %s", self.prompt_field, save.save_id)
            except Exception as exc:
                state.status = StageStatus.FAILED
                state.error = str(exc) or type(exc).__name__
                raise
            finally:
                state.finished_at = utc_now()
                store.save_manifest(reel)


class EnvironmentPromptStage(PromptStage):
    name = "environment_prompts"
    prompt_field = "environment_prompt"


class LabelPromptStage(PromptStage):
    name = "label_prompts"
    prompt_field = "label_prompt"


class BrollStillPromptStage(PromptStage):
    name = "broll_still_prompts"
    prompt_field = "broll_still_prompt"
