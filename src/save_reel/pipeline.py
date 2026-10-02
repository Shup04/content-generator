"""Concept generation followed by an ordered, injectable set of reel stages."""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from save_reel.models import (
    ConceptRequest,
    Reel,
    ReelConcept,
    SaveGame,
    StageState,
    StageStatus,
    utc_now,
)
from save_reel.prompting import BrollStillPromptCompiler, LabelPromptCompiler
from save_reel.providers.base import ConceptProvider
from save_reel.stages import BrollStillPromptStage, LabelPromptStage, ReelStage
from save_reel.storage import RunStore


@contextmanager
def run_logging(store: RunStore) -> Iterator[logging.Logger]:
    logger = logging.getLogger(f"save_reel.run.{store.run_dir.name}")
    handler = logging.FileHandler(store.run_dir / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


class CreatePipeline:
    def __init__(
        self, provider: ConceptProvider, *, stages: Sequence[ReelStage] | None = None
    ) -> None:
        self.provider = provider
        self.stages = (
            tuple(stages)
            if stages is not None
            else (
                LabelPromptStage(LabelPromptCompiler()),
                BrollStillPromptStage(BrollStillPromptCompiler()),
            )
        )
        names = [stage.name for stage in self.stages]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("stage names must be nonempty and unique")
        if "concept_generation" in names:
            raise ValueError("concept_generation is reserved for the concept provider")

    def create(
        self,
        request: ConceptRequest,
        *,
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
    ) -> Reel:
        store = RunStore.create(runs_dir, run_id)
        return self.create_in_store(request, store)

    def create_in_store(self, request: ConceptRequest, store: RunStore) -> Reel:
        """Compile a prepared concept in an existing, exclusively owned run directory.

        Story resume uses this with cached concepts; no media stage is added here.
        """
        with run_logging(store) as logger:
            logger.info(
                "Created run %s with concept provider %s", store.run_dir.name, self.provider.name
            )
            started_at = utc_now()
            try:
                concept = ReelConcept.model_validate(self.provider.generate_concept(request))
            except Exception:
                logger.exception("Concept generation failed before a valid reel could be created")
                raise
            reel = Reel(
                run_id=store.run_dir.name,
                request=request,
                concept_provider=self.provider.name,
                title=concept.title,
                saves=tuple(
                    SaveGame(save_id=f"save_{index:02d}", **save.model_dump())
                    for index, save in enumerate(concept.saves, start=1)
                ),
                status=StageStatus.RUNNING,
                stages={
                    "concept_generation": StageState(
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        finished_at=utc_now(),
                    ),
                    **{stage.name: StageState() for stage in self.stages},
                },
            )
            for save in reel.saves:
                store.save_directory(save.save_id)
            store.save_manifest(reel)
            logger.info("Concept generation completed with four saves")
            for stage in self.stages:
                state = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                reel.stages[stage.name] = state
                store.save_manifest(reel)
                logger.info("Starting stage %s", stage.name)
                try:
                    stage.run(reel, store, logger)
                except Exception as exc:
                    state.status = StageStatus.FAILED
                    state.error = str(exc) or type(exc).__name__
                    state.finished_at = utc_now()
                    reel.status = StageStatus.FAILED
                    store.save_manifest(reel)
                    logger.exception(
                        "Stage %s failed; manifest: %s", stage.name, store.manifest_path
                    )
                    raise
                state.status = StageStatus.COMPLETED
                state.finished_at = utc_now()
                store.save_manifest(reel)
                logger.info("Completed stage %s", stage.name)
            reel.status = StageStatus.COMPLETED
            store.save_manifest(reel)
            logger.info("Run completed; manifest: %s", store.manifest_path)
            return reel
