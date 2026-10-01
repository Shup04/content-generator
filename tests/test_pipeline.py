import hashlib
import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from save_reel.models import Reel, StageStatus
from save_reel.pipeline import CreatePipeline
from save_reel.prompting import (
    BrollStillPromptCompiler,
    EnvironmentPromptCompiler,
    LabelPromptCompiler,
)
from save_reel.providers import MockConceptProvider
from save_reel.stages import BrollStillPromptStage, EnvironmentPromptStage, LabelPromptStage
from save_reel.storage import RunStore


def test_create_persists_complete_reel(tmp_path, request_model):
    with patch("socket.socket", side_effect=AssertionError("mock must remain offline")):
        reel = CreatePipeline(MockConceptProvider()).create(
            request_model, runs_dir=tmp_path, run_id="demo"
        )
    store = RunStore(tmp_path / "demo")
    assert store.load_manifest() == reel
    assert reel.status == StageStatus.COMPLETED
    assert len(reel.saves) == 4
    assert all(state.status == StageStatus.COMPLETED for state in reel.stages.values())
    for save in reel.saves:
        for kind in ("label", "broll_still"):
            field = f"{kind}_prompt"
            artifact = save.artifacts[field]
            path = store.run_dir / artifact.path
            assert path == store.run_dir / "saves" / save.save_id / f"{field}.txt"
            assert path.read_text() == getattr(save, field).text
            assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
            assert save.stages[f"{kind}_prompts"].status == StageStatus.COMPLETED
        assert save.environment_prompt is None
    log = (store.run_dir / "run.log").read_text()
    assert "Run completed" in log
    assert all(save.save_id in log for save in reel.saves)
    assert len(list(store.run_dir.glob("saves/*/*_prompt.txt"))) == 8
    assert not logging.getLogger("save_reel.run.demo").handlers


def test_existing_run_is_not_overwritten(tmp_path, request_model):
    pipeline = CreatePipeline(MockConceptProvider())
    pipeline.create(request_model, runs_dir=tmp_path, run_id="same")
    manifest = tmp_path / "same" / "manifest.json"
    original = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        pipeline.create(request_model, runs_dir=tmp_path, run_id="same")
    assert manifest.read_bytes() == original


def test_generated_run_ids_are_unique(tmp_path, request_model):
    pipeline = CreatePipeline(MockConceptProvider())
    first = pipeline.create(request_model, runs_dir=tmp_path)
    second = pipeline.create(request_model, runs_dir=tmp_path)
    assert first.run_id != second.run_id
    assert RunStore(tmp_path / first.run_id).load_manifest() == first
    assert RunStore(tmp_path / second.run_id).load_manifest() == second


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", "bad/name", "bad name", ""])
def test_invalid_run_id_cannot_create_files(tmp_path, run_id):
    with pytest.raises(ValidationError):
        RunStore.create(tmp_path / "runs", run_id)
    assert not (tmp_path / "runs").exists()


def test_failed_stage_preserves_partial_progress(tmp_path, request_model):
    class FailingCompiler(EnvironmentPromptCompiler):
        def compile(self, request, save):
            if save.save_id == "save_03":
                raise RuntimeError("intentional failure")
            return super().compile(request, save)

    pipeline = CreatePipeline(
        MockConceptProvider(), stages=(EnvironmentPromptStage(FailingCompiler()),)
    )
    with pytest.raises(RuntimeError, match="intentional failure"):
        pipeline.create(request_model, runs_dir=tmp_path, run_id="failed")
    reel = RunStore(tmp_path / "failed").load_manifest()
    assert reel.status == StageStatus.FAILED
    state = reel.stages["environment_prompts"]
    assert state.status == StageStatus.FAILED
    assert state.error == "intentional failure"
    assert state.started_at <= state.finished_at
    assert reel.saves[0].stages["environment_prompts"].status == StageStatus.COMPLETED
    assert reel.saves[2].stages["environment_prompts"].status == StageStatus.FAILED
    assert reel.saves[3].stages["environment_prompts"].status == StageStatus.PENDING
    assert reel.saves[3].environment_prompt is None
    assert len(list((tmp_path / "failed").glob("saves/*/environment_prompt.txt"))) == 2
    assert "intentional failure" in (tmp_path / "failed" / "run.log").read_text()
    assert not logging.getLogger("save_reel.run.failed").handlers


def test_provider_failure_leaves_diagnostic_log(tmp_path, request_model):
    class BrokenProvider:
        name = "broken"

        def generate_concept(self, request):
            return {"title": "Invalid", "saves": []}

    with pytest.raises(ValidationError):
        CreatePipeline(BrokenProvider()).create(request_model, runs_dir=tmp_path, run_id="invalid")
    assert not (tmp_path / "invalid" / "manifest.json").exists()
    assert "Concept generation failed" in (tmp_path / "invalid" / "run.log").read_text()


def test_provider_and_new_stages_are_injectable(tmp_path, request_model, concept):
    class FixedProvider:
        name = "fixture"

        def generate_concept(self, request):
            assert request == request_model
            return concept

    class SummaryStage:
        name = "summary"

        def run(self, reel, store, logger):
            assert all(save.environment_prompt for save in reel.saves)
            for save in reel.saves:
                save.artifacts["summary"] = store.write_text(
                    f"saves/{save.save_id}/summary.txt", save.summary
                )

    reel = CreatePipeline(
        FixedProvider(),
        stages=(EnvironmentPromptStage(EnvironmentPromptCompiler()), SummaryStage()),
    ).create(request_model, runs_dir=tmp_path, run_id="extended")
    assert reel.concept_provider == "fixture"
    assert reel.stages["summary"].status == StageStatus.COMPLETED
    assert all("summary" in save.artifacts for save in reel.saves)
    assert RunStore(tmp_path / "extended").load_manifest() == reel


def test_stage_names_must_be_unique():
    stage = EnvironmentPromptStage(EnvironmentPromptCompiler())
    with pytest.raises(ValueError, match="unique"):
        CreatePipeline(MockConceptProvider(), stages=(stage, stage))


def test_broll_failure_preserves_all_completed_label_prompts(tmp_path, request_model):
    class FailingCompiler(BrollStillPromptCompiler):
        def compile(self, request, save):
            if save.save_id == "save_03":
                raise RuntimeError("B-roll compilation failed")
            return super().compile(request, save)

    pipeline = CreatePipeline(
        MockConceptProvider(),
        stages=(LabelPromptStage(LabelPromptCompiler()), BrollStillPromptStage(FailingCompiler())),
    )
    with pytest.raises(RuntimeError, match="B-roll compilation failed"):
        pipeline.create(request_model, runs_dir=tmp_path, run_id="failed-broll")
    reel = RunStore(tmp_path / "failed-broll").load_manifest()
    assert reel.status == StageStatus.FAILED
    assert reel.stages["label_prompts"].status == StageStatus.COMPLETED
    assert reel.stages["broll_still_prompts"].status == StageStatus.FAILED
    assert all(save.label_prompt for save in reel.saves)
    assert sum(save.broll_still_prompt is not None for save in reel.saves) == 2


def test_original_manifests_still_load(tmp_path, request_model):
    reel = CreatePipeline(
        MockConceptProvider(), stages=(EnvironmentPromptStage(EnvironmentPromptCompiler()),)
    ).create(request_model, runs_dir=tmp_path, run_id="legacy")
    original = reel.model_dump(mode="json")
    for save in original["saves"]:
        del save["label_prompt"]
        del save["broll_still_prompt"]
        for field in ("world_scene", "shot_composition", "key_surfaces"):
            del save["environment"][field]
    loaded = Reel.model_validate(original)
    assert len(loaded.saves) == 4
    assert all(save.environment_prompt for save in loaded.saves)
    assert all(save.label_prompt is None for save in loaded.saves)
