"""The final clarity edit is cached, isolated, and used by the speech handoff."""

# ruff: noqa: F811 -- shared pytest fixtures
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_studio import prepare_job, store  # noqa: F401

from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_editor import check_text, maximum_words
from save_reel.story_export import review_text, visual_values
from save_reel.story_models import StoryRun, StorySettings, word_count
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prose_models import ProseParagraph
from save_reel.studio_store import games_from_story


class Editor(MockStoryProvider):
    def __init__(self):
        super().__init__()
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["stage"] == "narration_editor":
            # Delete the remaining sentences without supplying any new facts.
            original = kwargs["context"]["final_narration"]
            return ProseParagraph(paragraph=original.split(". ")[0].rstrip(".") + ".")
        return super().generate(**kwargs)


@pytest.fixture
def edited(tmp_path):
    provider = Editor()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="edited", random_seed=41)
    return workflow, provider, run, tmp_path / "edited"


def test_editor_only_receives_concept_and_selected_narration(edited):
    _, provider, run, path = edited
    calls = [c for c in provider.calls if c["stage"] == "narration_editor"]
    assert len(calls) == 4
    for call, slot in zip(calls, run.saves, strict=True):
        assert call["response_type"] is ProseParagraph
        limit = maximum_words(
            (slot.travelogue.selected.paragraph,), slot.spoken_policy(run.settings)
        )
        assert call["context"] == {
            "selected_world_concept": slot.selected.model_dump(mode="json"),
            "final_narration": slot.travelogue.selected.paragraph,
            "length_limit": (
                f"Return at most {limit} "
                "words. This is a hard maximum, not a target. Shorter is allowed; "
                "do not add details to reach the limit."
            ),
        }
        index = provider.calls.index(call)
        assert provider.calls[index - 1]["stage"] == "narration_prose_selection"
        assert "Do not change the concept or invent lore" in call["prompt"]
        assert "quantities, timing, frequency, conditions, or consequences" in call["prompt"]
        assert "grade 3–4" in call["prompt"]
        assert "Return only the cleaned narration" in call["prompt"]
        assert slot.final.brief.narration == (slot.script_edit.narration,)
        assert slot.script_edit.source_narration == (slot.travelogue.selected.paragraph,)
        assert slot.narration_stats.words == word_count(slot.final.brief.narration) < 45
        assert not slot.narration_stats.within_target
    assert "SCRIPT EDITOR" in review_text(run)
    manifest = RunStore(path).load_manifest()
    assert any(t.name == "story_narration_editor" and t.version == "v1"
               for t in manifest.story_generation.templates)
    for save in manifest.saves:
        assert (path / save.artifacts["script_edit"].path).is_file()


def test_editor_does_not_change_world_novelty_or_visuals(edited, tmp_path):
    _, edited_provider, run, _ = edited
    provider = Editor()
    original = StoryWorkflow(provider).create(
        ConceptRequest(), runs_dir=tmp_path / "original", run_id="unedited", random_seed=41,
        settings=StorySettings(script_editor_prompt_version=None),
    )
    assert not any(c["stage"] == "narration_editor" for c in provider.calls)
    assert provider.calls == [c for c in edited_provider.calls if c["stage"] != "narration_editor"]
    for before, after in zip(original.saves, run.saves, strict=True):
        assert before.selected == after.selected
        assert before.world == after.world
        assert before.assessments == after.assessments
        assert before.travelogue == after.travelogue
        assert visual_values(before) == visual_values(after)
        assert before.final.environment.broll_beats == after.final.environment.broll_beats


def test_resume_and_one_save_rewrite_cache_edits(edited):
    workflow, provider, old, path = edited
    provider.calls.clear()
    assert workflow.resume(path) == old
    assert not provider.calls
    new = workflow.regenerate(path, scope="narration", save_id="save_02", run_id="rewrite")
    assert [c["stage"] for c in provider.calls].count("narration_editor") == 1
    assert all(c["stage"].startswith("narration") for c in provider.calls)
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]
    assert new.saves[1].world == old.saves[1].world


def test_disabling_editor_for_one_rewrite_retains_other_saved_edits(edited):
    workflow, provider, old, path = edited
    provider.calls.clear()
    new = workflow.rewrite_narration(
        path, runs_dir=path.parent, run_id="editor-off", save_ids=("save_02",),
        settings=StorySettings(script_editor_prompt_version=None),
    )
    assert new.saves[1].script_edit is None
    assert not any(c["stage"] == "narration_editor" for c in provider.calls)
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]


def test_completed_response_survives_interruption_before_editor_checkpoint(tmp_path):
    class Interrupt(StoryWorkflow):
        interrupted = False

        def _save(self, run, store):
            if run.saves[0].script_edit and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt("Interrupted after the response was saved")
            return super()._save(run, store)

    provider = Editor()
    workflow = Interrupt(provider)
    with pytest.raises(KeyboardInterrupt):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="interrupted")
    path = tmp_path / "interrupted"
    before = workflow.load(path)
    assert before.saves[0].script_edit is None
    assert before.saves[0].travelogue.selected is not None
    provider.calls.clear()
    run = workflow.resume(path)
    edits = [c for c in provider.calls if c["stage"] == "narration_editor"]
    assert len(edits) == 3  # First save's already-paid result comes from the request cache.
    assert all(c["context"]["selected_world_concept"]["title"] != run.saves[0].selected.title
               for c in edits)


def test_stale_edit_and_skipped_editor_fail_manifest_validation(edited):
    raw = edited[2].model_dump()
    raw["saves"][0]["script_edit"]["source_narration"] = ["A different source script."]
    with pytest.raises(ValidationError, match="no longer matches"):
        StoryRun.model_validate(raw)
    raw = edited[2].model_dump()
    raw["saves"][0]["script_edit"] = None
    with pytest.raises(ValidationError, match="requires the configured script editor"):
        StoryRun.model_validate(raw)


def test_bad_editor_response_fails_without_finalizing_script(tmp_path):
    class EmptyEditor(Editor):
        def generate(self, **kwargs):
            if kwargs["stage"] == "narration_editor":
                return {"paragraph": ""}
            return super().generate(**kwargs)

    workflow = StoryWorkflow(EmptyEditor())
    with pytest.raises(ValueError, match="narration_editor validation exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="invalid",
                        settings=StorySettings(validation_retries=1))
    run = workflow.load(tmp_path / "invalid")
    assert run.saves[0].final is None and run.saves[0].script_edit is None
    assert run.saves[0].travelogue.selected is not None
    requests = tmp_path / "invalid/story_requests/save_01/narration_editor"
    assert len(list(requests.glob("*.json"))) == 2


@pytest.mark.parametrize("source_words,limit", [(65, 70), (80, 80)])
def test_editor_word_limit_is_explicit_and_still_enforced(source_words, limit):
    source = (" ".join(["word"] * source_words),)
    policy = SimpleNamespace(target_max_words=70)
    check_text(" ".join(["word"] * limit), source, policy)
    check_text("A short clear sentence.", source, policy)
    with pytest.raises(ValueError, match=rf"has {limit + 1} words; the maximum is {limit}"):
        check_text(" ".join(["word"] * (limit + 1)), source, policy)


def test_overlong_editor_repairs_its_rejected_text_and_retains_diagnostics(tmp_path):
    class OverlongEditor(Editor):
        rejected = None

        def generate(self, **kwargs):
            if kwargs["stage"] == "narration_editor" and (
                kwargs["context"]["selected_world_concept"]["candidate_id"].startswith("save_04_")
            ):
                assert "Return at most 50 words" in kwargs["prompt"]
                if self.rejected is None:
                    self.rejected = " ".join(["extra"] * 75)
                    self.calls.append(copy.deepcopy(kwargs))
                    return ProseParagraph(paragraph=self.rejected)
                assert "has 75 words; the maximum is 50" in kwargs["prompt"]
                assert "Shorten the edit by at least 25 words" in kwargs["prompt"]
                assert self.rejected in kwargs["prompt"]
            return super().generate(**kwargs)

    provider = OverlongEditor()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="repaired", random_seed=41)
    assert run.status == "completed"
    attempts = tmp_path / "repaired/story_requests/save_04/narration_editor"
    rejected = json.loads((attempts / "attempt_001.json").read_text())
    accepted = json.loads((attempts / "attempt_002.json").read_text())
    assert rejected["status"] == "invalid"
    assert rejected["response"]["paragraph"] == provider.rejected
    assert accepted["status"] == "completed"
    assert run.saves[3].script_edit.narration == accepted["response"]["paragraph"]


def test_resuming_editor_length_failure_preserves_three_completed_saves(tmp_path):
    class FailingFourthEditor(Editor):
        fail = True

        def generate(self, **kwargs):
            if self.fail and kwargs["stage"] == "narration_editor" and (
                kwargs["context"]["selected_world_concept"]["candidate_id"].startswith("save_04_")
            ):
                return ProseParagraph(paragraph=" ".join(["extra"] * 75))
            return super().generate(**kwargs)

    provider = FailingFourthEditor()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="save_04 narration_editor validation exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="resume-editor", random_seed=41,
                        settings=StorySettings(validation_retries=1))
    path = tmp_path / "resume-editor"
    before = workflow.load(path)
    assert all(s.final and s.script_edit for s in before.saves[:3])
    assert before.saves[3].final is None
    provider.fail = False
    provider.calls.clear()
    after = workflow.resume(path)
    assert after.status == "completed"
    assert after.saves[:3] == before.saves[:3]
    assert after.saves[3].world == before.saves[3].world
    assert [c["stage"] for c in provider.calls] == ["narration_editor"]
    assert "Return at most 50 words" in provider.calls[0]["prompt"]


def test_legacy_saved_runs_do_not_gain_editor_calls(tmp_path):
    provider = Editor()
    workflow = StoryWorkflow(provider)
    workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="legacy",
                    settings=StorySettings(script_editor_prompt_version=None))
    path = tmp_path / "legacy"
    raw = json.loads((path / "story.json").read_text())
    del raw["settings"]["script_editor_prompt_version"]
    for slot in raw["saves"]:
        del slot["script_edit"]
    # Regeneration uses the legacy source policy; no completed artifact is overwritten.
    (path / "story.json").write_text(json.dumps(raw))
    assert workflow.load(path).settings.script_editor_prompt_version is None
    provider.calls.clear()
    run = workflow.regenerate(path, scope="narration", save_id="save_02", run_id="legacy-rewrite")
    assert all(s.script_edit is None for s in run.saves)
    assert not any(c["stage"] == "narration_editor" for c in provider.calls)


def test_tts_handoff_receives_cleaned_text(edited, store, monkeypatch):
    draft = store.create()
    draft.games = games_from_story(edited[2])
    draft.last_render = "existing-render"
    draft = store.save(draft)
    received = []

    class SpeechBoundary:
        def __init__(self, provider):
            pass

        def prepare(self, source, script, *args, **kwargs):
            received.append(script)

        def load(self, store):
            return SimpleNamespace(cues=[])

        def execute(self, *args, **kwargs):
            pass

    monkeypatch.setattr("save_reel.narration.NarrationPipeline", SpeechBoundary)
    prepare_job(store, draft, "narrate").narrate()
    assert [g.text for g in received[0].games] == [s.script_edit.narration for s in edited[2].saves]
    assert all(g.text != s.travelogue.selected.paragraph
               for g, s in zip(received[0].games, edited[2].saves, strict=True))


def test_editor_uses_configured_narration_model_and_reasoning():
    pytest.importorskip("openai")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_parsed=ProseParagraph(paragraph="You wait for the door."))

    settings = StorySettings(narration_model="configured-writer", narration_reasoning_effort="low")
    provider = OpenAIStoryProvider(settings, client=SimpleNamespace(
        responses=SimpleNamespace(parse=parse),
    ))
    provider.generate(stage="narration_editor", prompt="Offline", context={},
                      response_type=ProseParagraph)
    assert calls[0]["model"] == "configured-writer"
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert set(calls[0]["text_format"].model_fields) == {"paragraph"}
