"""Production must write real scripts before paying to voice offline fixtures."""

# ruff: noqa: F811 -- shared pytest fixtures

import json

import pytest
from test_studio import prepare_job, store, story  # noqa: F401

from save_reel.providers.mock_story import MockStoryProvider
from save_reel.story_models import SAVE_IDS, StorySettings
from save_reel.story_pipeline import StoryWorkflow
from save_reel.studio_store import games_from_story, write_json
from save_reel.studio_worker import ProductionJob


class FakeWriter(MockStoryProvider):
    """Fake the live text boundary; all tests remain offline."""

    name = "openai"

    def __init__(self):
        super().__init__()
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        result = super().generate(**kwargs)
        if kwargs["stage"] == "narration_prose_candidate":
            result.paragraph = kwargs["context"]["title"] + ": " + result.paragraph
        return result


def source_draft(store, story):
    target = store.run_dir(story.run_id)
    target.mkdir()
    (target / "story.json").write_text(story.model_dump_json())
    draft = store.create()
    draft.provider = "openai"  # Switching this must not relabel cached mock text.
    draft.games = games_from_story(story)
    draft.source_story = story.run_id
    for i, game in enumerate(draft.games, 1):
        game.cartridge_run = f"original-cart{i}"
        game.broll_run = f"original-video{i}"
    return store.save(draft)


def test_legacy_mock_origin_is_recovered_and_manual_edits_preserved(store, story):
    draft = source_draft(store, story)
    legacy = draft.model_dump(mode="json")
    for game in legacy["games"]:
        game.pop("narration_origin")
    legacy["games"][1]["narration"] = "A script I wrote myself."
    write_json(store.drafts / f"{draft.draft_id}.json", legacy)
    loaded = store.load(draft.draft_id)
    assert loaded.provider == "openai"
    assert [g.needs_script for g in loaded.games] == [True, False, True, True]
    assert loaded.games[1].narration_origin.provider == "manual"
    loaded.games[0].narration = "Another manual script."
    saved = store.save(loaded)
    assert not saved.games[0].needs_script


def test_rewrite_uses_writer_only_preserves_worlds_assets_and_cache(store, story, monkeypatch):
    draft = source_draft(store, story)
    writer = FakeWriter()
    monkeypatch.setattr("save_reel.story_cli._provider", lambda name, *a: writer)
    job = prepare_job(store, draft, "scripts")
    job.execute()
    assert job.job["status"] == "completed", job.job.get("error")
    assert len([c for c in writer.calls if c["stage"] == "narration_prose_candidate"]) == 12
    assert all(c["stage"].startswith("narration") for c in writer.calls)
    branch = StoryWorkflow.load(store.run_dir(job.draft.source_story))
    assert branch.provider == "mock"  # World provenance is not rewritten.
    assert all(s.narration_provider == "openai" for s in branch.saves)
    for before, after, original, rewritten in zip(
        draft.games, job.draft.games, story.saves, branch.saves, strict=True
    ):
        assert before.model_dump(exclude={"narration", "narration_origin"}) == after.model_dump(
            exclude={"narration", "narration_origin"}
        )
        assert original.world == rewritten.world
        assert original.selected == rewritten.selected
        assert after.narration_origin.model == "gpt-6.1-sol"
        assert after.narration != before.narration
    assert not (store.runs / ".story-history").exists()
    records = list(store.run_dir(branch.run_id).glob("story_requests/*/*/*.json"))
    assert all(json.loads(p.read_text())["provider"] == "openai" for p in records)
    assert all(json.loads(p.read_text())["model"] == "gpt-6.1-sol" for p in records)
    writer.calls.clear()
    ProductionJob(store, job.job_id).scripts(force=True)
    assert not writer.calls


def test_full_rewrites_mock_before_media_and_voice(store, story, monkeypatch):
    draft = source_draft(store, story)
    events = []
    writer = FakeWriter()
    monkeypatch.setattr("save_reel.story_cli._provider", lambda *a: writer)

    def media(self, kind):
        assert len(writer.calls) == 20
        assert all(not g.needs_script for g in self.draft.games)
        events.append(kind)

    def narrate(self):
        assert len({g.narration for g in self.draft.games}) == 4
        events.append("speech")
        self.job["outputs"]["reel"] = "fake-reel"

    monkeypatch.setattr(ProductionJob, "media", media)
    monkeypatch.setattr(ProductionJob, "render", lambda self: events.append("render"))
    monkeypatch.setattr(ProductionJob, "narrate", narrate)
    monkeypatch.setattr("save_reel.studio_store.is_finished_reel", lambda *a: True)
    job = prepare_job(store, draft, "full")
    job.execute()
    assert job.job["status"] == "completed", job.job.get("error")
    assert events == ["cartridges", "stills", "videos", "render", "speech"]
    assert "scripts" in job.job["outputs"]


def test_duplicate_manual_scripts_stop_before_paid_work(store, story, monkeypatch):
    draft = source_draft(store, story)
    for game in draft.games:
        game.narration = "The same custom script in every slot."
    draft = store.save(draft)
    monkeypatch.setattr(ProductionJob, "media", lambda *a: pytest.fail("Paid media called"))
    monkeypatch.setattr(ProductionJob, "narrate", lambda *a: pytest.fail("Paid speech called"))
    job = prepare_job(store, draft, "full")
    job.execute()
    assert job.job["status"] == "failed"
    assert "identical narration" in job.job["error"]


def test_automatic_rewrite_keeps_manual_script_and_stops_on_writer_failure(
    store, story, monkeypatch,
):
    draft = source_draft(store, story)
    draft.games[0].narration = "My own narration stays exactly as written."
    draft = store.save(draft)
    writer = FakeWriter()
    monkeypatch.setattr("save_reel.story_cli._provider", lambda *a: writer)
    job = prepare_job(store, draft, "narrate")
    job.scripts()
    assert job.draft.games[0].narration == draft.games[0].narration
    assert len(writer.calls) == 15  # Only three saves needed a writer.
    writer.calls.clear()
    job.scripts()
    assert not writer.calls

    def unavailable(*args):
        raise RuntimeError("Writer unavailable")

    monkeypatch.setattr("save_reel.story_cli._provider", unavailable)
    monkeypatch.setattr(ProductionJob, "media", lambda *a: pytest.fail("Paid media called"))
    monkeypatch.setattr(ProductionJob, "narrate", lambda *a: pytest.fail("Paid speech called"))
    failed = prepare_job(store, draft, "full", job_id="failed-writer")
    failed.execute()
    assert failed.job["status"] == "failed"
    assert failed.job["error"] == "Writer unavailable"


def test_partial_rewrite_resume_preserves_three_scripts_and_finished_requests(store, story):
    source_draft(store, story)

    class InterruptedWriter(FakeWriter):
        interrupted = False

        def generate(self, **kwargs):
            if kwargs["stage"] == "narration_prose_candidate" and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("interrupted")
            return super().generate(**kwargs)

    writer = InterruptedWriter()
    workflow = StoryWorkflow(writer)
    arguments = dict(runs_dir=store.runs, run_id="rewrite", settings=StorySettings(),
                     save_ids=(SAVE_IDS[0],))
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.rewrite_narration(store.run_dir(story.run_id), **arguments)
    writer.calls.clear()
    run = workflow.rewrite_narration(store.run_dir(story.run_id), **arguments)
    assert run.saves[1:] == story.saves[1:]
    assert len(writer.calls) == 4  # Description was already saved.
    assert all(c["stage"].startswith("narration") for c in writer.calls)
    writer.calls.clear()
    workflow.rewrite_narration(store.run_dir(story.run_id), **arguments)
    assert not writer.calls
