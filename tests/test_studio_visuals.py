"""Additional shots preserve saved media and do not repeat paid planning on resume."""

# ruff: noqa: F811 -- shared pytest fixtures
import json

import pytest
from test_studio import prepare_job, store, story  # noqa: F401

from save_reel.broll import BrollPipeline
from save_reel.media_models import BrollSettings
from save_reel.providers.media import GeneratedImage, VideoTask
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.studio_store import games_from_story, snapshot_prompts
from save_reel.studio_visuals import plan_views
from save_reel.studio_worker import ProductionJob


@pytest.mark.parametrize("legacy", [False, True])
def test_append_views_retains_existing_values_and_reuses_checkpoint(store, story, tmp_path, legacy):
    draft = store.create()
    game = games_from_story(story)[0]
    game.broll_beats = () if legacy else game.broll_beats[:2]
    game.set_clip_run(1, "approved-first")
    before = game.model_copy(deep=True)
    original_values = game.clip_values(1)
    prompts = tmp_path / "prompts"
    snapshot_prompts(draft, prompts)

    class Planner(MockStoryProvider):
        calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            assert kwargs["context"]["additional_view_count"] == (3 if legacy else 2)
            assert kwargs["context"]["approved_world"] == {"approved": "facts"}
            assert "No people" in kwargs["prompt"]
            return super().generate(**kwargs)

    provider = Planner()
    for target in (game, before):  # Simulate a crash after response but before draft checkpoint.
        plan_views(target, 4, settings=draft.story, prompts=prompts, folder=tmp_path,
                   provider=provider, world={"approved": "facts"})
        assert len(target.broll_beats) == 4
        assert target.clip_run(1) == "approved-first"
        assert target.clip_values(1) == original_values
    assert provider.calls == 1
    assert game == before
    plan_views(game, 4, settings=draft.story, prompts=prompts, folder=tmp_path,
               provider=provider)
    assert provider.calls == 1


def test_ambiguous_planning_request_is_not_repeated(store, story, tmp_path):
    draft = store.create()
    game = games_from_story(story)[0]
    game.broll_beats = game.broll_beats[:2]
    snapshot_prompts(draft, tmp_path / "prompts")

    class Interrupted:
        calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            raise RuntimeError("connection interrupted")

    provider = Interrupted()
    for error, message in ((RuntimeError, "connection interrupted"),
                           (ValueError, "will not retry automatically")):
        with pytest.raises(error, match=message):
            plan_views(game, 4, settings=draft.story, prompts=tmp_path / "prompts",
                       folder=tmp_path, provider=provider)
    assert provider.calls == 1
    assert len(game.broll_beats) == 2


def test_one_video_three_stills_expands_existing_world_and_caches_all_four(
    store, story, monkeypatch,
):
    draft = store.create()
    draft.games = games_from_story(story)
    draft.broll = BrollSettings(visual_mode="mixed", stills_per_save=1)
    for game in draft.games:
        game.broll_beats = game.broll_beats[:2]
    draft.parallel_saves = 1
    draft = store.save(draft)
    calls = {"image": 0, "video": 0}

    class Image:
        def generate(self, prompt, settings):
            calls["image"] += 1
            return GeneratedImage(b"offline-image")

    class Video:
        def submit(self, image, prompt, settings):
            calls["video"] += 1
            assert settings.video_model == "MiniMax-H3"
            return "offline-task"

        def query(self, task):
            return VideoTask("succeeded", "https://test.invalid/video")

        def download(self, url):
            return b"offline-video"

    def command(self, verb, target, *args):
        BrollPipeline(Image(), Video()).execute(RunStore(target), image_only="--image-only" in args)

    monkeypatch.setattr(ProductionJob, "command", command)
    first = prepare_job(store, draft, "videos")
    first.execute()
    assert first.job["status"] == "completed", first.job.get("error")
    assert calls == {"image": 8, "video": 4}
    before = store.load(draft.draft_id)
    old_beats = [g.broll_beats for g in before.games]
    before.broll.stills_per_save = 3
    expanded = prepare_job(store, store.save(before), "videos", job_id="expanded")
    expanded.execute()
    assert expanded.job["status"] == "completed", expanded.job.get("error")
    assert expanded.job["progress"]["completed"] == expanded.job["progress"]["total"] == 20
    assert calls == {"image": 16, "video": 4}
    saved = store.load(draft.draft_id)
    for game, original in zip(saved.games, old_beats, strict=True):
        assert game.broll_beats[:2] == original
        for beat in (3, 4):
            run = BrollPipeline.load(RunStore(store.runs / game.clip_run(beat)))
            assert run.still_filename == f"broll_{beat:02}_still.png"
            assert "video" not in run.artifacts
    cached = prepare_job(store, saved, "videos", job_id="cached")
    cached.execute()
    assert cached.job["status"] == "completed", cached.job.get("error")
    assert calls == {"image": 16, "video": 4}
    assert len(list(expanded.folder.glob("views-*.json"))) == 4
    for path in expanded.folder.glob("views-*.json"):
        assert json.loads(path.read_text())["status"] == "received"
    selective = prepare_job(store, store.load(draft.draft_id), "stills", job_id="fourth",
                            save_number=2, beat_number=4, regenerate=True)
    selective.execute()
    assert selective.job["status"] == "completed", selective.job.get("error")
    assert calls == {"image": 17, "video": 4}
    updated = store.load(draft.draft_id)
    assert updated.games[1].clip_run(4) != saved.games[1].clip_run(4)
    assert updated.games[1].broll_beats[:3] == saved.games[1].broll_beats[:3]


def test_render_does_not_silently_omit_missing_views(store, story):
    draft = store.create()
    draft.games = games_from_story(story)
    for game in draft.games:
        game.broll_beats = game.broll_beats[:2]
    draft = store.save(draft)
    job = prepare_job(store, draft, "render")
    job.execute()
    assert job.job["status"] == "failed"
    assert "needs additional views" in job.job["error"]
