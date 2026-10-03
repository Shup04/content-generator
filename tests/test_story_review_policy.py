"""Review-policy upgrades preserve creative work and cannot reset budgets on resume."""

import json

import pytest

from save_reel.cli import build_parser
from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.story_export import review_text
from save_reel.story_models import StorySettings
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_review_policy import prepare_world_recheck
from save_reel.studio_models import StudioDraft
from save_reel.studio_store import StudioStore, snapshot_prompts, validate_prompts


class Critic(MockStoryProvider):
    def __init__(self, *, always_reject=False):
        super().__init__()
        self.calls = []
        self.always_reject = always_reject

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        result = super().generate(**kwargs)
        if kwargs["stage"] == "world_review" and kwargs["context"]["save_id"] == "save_01":
            if self.always_reject or "not real-world feasibility" not in kwargs["prompt"]:
                result.practical_system = False
                result.issues = ("The selected rule and consequence contradict one another.",)
        return result


@pytest.fixture
def failed(tmp_path):
    provider = Critic()
    workflow = StoryWorkflow(provider, max_workers=4)
    path = tmp_path / "old-review"
    with pytest.raises(ValueError, match="world simulation approval exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id=path.name,
                        settings=StorySettings(
                            validation_retries=0, max_world_replacements=0,
                            world_review_prompt_version=None, world_review_reasoning_effort=None,
                            travelogue=None,
                        ))
    return workflow, provider, path


def test_upgrade_reviews_saved_draft_before_rewrite_and_keeps_approved_worlds(failed):
    workflow, provider, path = failed
    old = workflow.load(path)
    provider.calls.clear()
    staged = prepare_world_recheck(path, version="v4")
    assert provider.calls == []
    assert staged.templates["world_review"].template.version == "v4"
    assert staged.settings.effort_for("world_review") == "medium"
    assert staged.settings.effort_for("world_simulation") == "high"
    for key in old.templates.keys() - {"world_review"}:
        assert staged.templates[key] == old.templates[key]
    assert staged.saves[1:] == old.saves[1:]
    assert staged.saves[0].world == old.saves[0].world_attempts[-1].simulation
    assert staged.saves[0].selected == old.saves[0].selected
    assert staged.world_review_updates[0].attempts == {"save_01": old.saves[0].world_attempts}
    assert staged.world_review_updates[0].previous_prompt == old.templates["world_review"]
    assert "save_01 archived review" in review_text(staged)
    run = workflow.resume(path)
    assert run.status == "completed"
    assert [c["stage"] for c in provider.calls].count("world_review") == 1
    assert not any(c["stage"] in ("candidates", "review", "world_simulation", "reel_review")
                   for c in provider.calls)
    assert run.saves[0].world == staged.saves[0].world
    provider.calls.clear()
    assert workflow.resume(path) == run
    assert not provider.calls


def test_rejections_still_block_and_same_policy_cannot_replenish_budget(failed):
    _, provider, path = failed
    provider.always_reject = True
    workflow = StoryWorkflow(provider, max_workers=4)
    prepare_world_recheck(path, version="v4")
    provider.calls.clear()
    with pytest.raises(ValueError, match="contradict one another"):
        workflow.resume(path)
    assert [c["stage"] for c in provider.calls] == ["world_review"]
    before = path.joinpath("story.json").read_bytes()
    prepare_world_recheck(path, version="v4")
    assert path.joinpath("story.json").read_bytes() == before
    provider.calls.clear()
    with pytest.raises(ValueError, match="world simulation approval exhausted"):
        workflow.resume(path)
    assert not provider.calls


def test_old_runs_keep_saved_policy_until_explicit_upgrade(failed):
    workflow, _, path = failed
    raw = json.loads(path.joinpath("story.json").read_text())
    del raw["settings"]["world_review_prompt_version"]
    del raw["settings"]["world_review_reasoning_effort"]
    del raw["world_review_updates"]
    path.joinpath("story.json").write_text(json.dumps(raw))
    run = workflow.load(path)
    assert run.settings.world_review_prompt_version is None
    assert run.settings.effort_for("world_review") == "high"
    assert run.templates["world_review"].template.version == "v3"
    assert run.world_review_updates == ()


def test_new_defaults_and_studio_prompt_snapshot(tmp_path):
    store = StudioStore(tmp_path)
    draft = store.create()
    assert draft.story.world_review_prompt_version == "v4"
    assert draft.story.effort_for("world_review") == "medium"
    snapshot_prompts(draft, tmp_path / "prompts")
    validate_prompts(draft, tmp_path / "prompts")
    assert (tmp_path / "prompts/story_world_review/v4.txt").exists()
    raw = draft.model_dump(mode="json")
    del raw["story"]["world_review_prompt_version"]
    del raw["story"]["world_review_reasoning_effort"]
    old = StudioDraft.model_validate(raw)
    assert old.story.world_review_prompt_version is None
    assert old.story.effort_for("world_review") == "high"


def test_custom_review_is_preserved_on_ordinary_resume(failed, tmp_path):
    workflow, provider, path = failed
    folder = tmp_path / "custom/story_world_review"
    folder.mkdir(parents=True)
    folder.joinpath("v5.txt").write_text("Custom consistency review. [CONTEXT] [FEEDBACK]")
    run = prepare_world_recheck(path, version="v5", effort="low", prompts_dir=folder.parent)
    assert run.templates["world_review"].template.version == "v5"
    assert run.settings.effort_for("world_review") == "low"
    folder.joinpath("v5.txt").write_text("Edited file must not replace the saved prompt.")
    with pytest.raises(ValueError, match="world simulation approval exhausted"):
        workflow.resume(path)
    assert workflow.load(path).templates["world_review"] == run.templates["world_review"]
    assert "Custom consistency review" in provider.calls[-1]["prompt"]


def test_cli_upgrade_is_explicit():
    parser = build_parser()
    default = parser.parse_args(["resume-concepts", "runs/test"])
    assert default.world_review_version is None
    explicit = parser.parse_args(["resume-concepts", "runs/test", "--world-review-version", "v4"])
    assert explicit.world_review_version == "v4"
    assert explicit.world_review_effort == "medium"
