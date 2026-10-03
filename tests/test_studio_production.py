"""Production orchestration, real local composition, and bounded parallel work."""

# ruff: noqa: F811 -- pytest injects the imported shared fixtures.

import json
import threading

import pytest
from pydantic import ValidationError
from test_narration import FakeSpeech, narration_assets  # noqa: F401
from test_rendering import local_assets, png_cutout  # noqa: F401
from test_studio import prepare_job, store, story  # noqa: F401

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.media_models import BrollSettings
from save_reel.models import ConceptRequest, StageState, StageStatus
from save_reel.polish_models import ReelPolish
from save_reel.providers.media import GeneratedImage, MediaError, VideoTask
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.render_models import RenderSettings
from save_reel.storage import RunStore
from save_reel.story_pipeline import StoryWorkflow
from save_reel.studio_store import games_from_story, is_finished_reel
from save_reel.studio_worker import ProductionJob


class ConcurrentStoryProvider(MockStoryProvider):
    def __init__(self, fail=False):
        super().__init__()
        self.barriers = {
            k: threading.Barrier(4, timeout=10)
            for k in (
                "candidates",
                "world_simulation",
                "narration_description",
            )
        }
        self.fail = fail
        self.calls = []

    def generate(self, **kwargs):
        stage = kwargs["stage"]
        self.calls.append(stage)
        if stage in self.barriers:
            self.barriers[stage].wait()
        if self.fail and stage == "world_simulation" and kwargs["context"]["save_id"] == "save_02":
            raise RuntimeError("A simulated provider failure")
        return super().generate(**kwargs)


def test_world_and_narration_requests_are_parallel_with_stage_barriers(tmp_path):
    provider = ConcurrentStoryProvider()
    events = []
    workflow = StoryWorkflow(provider, max_workers=4, progress=lambda *v: events.append(v))
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="parallel", random_seed=41)
    assert run.status == "completed"
    first_script = next(i for i, e in enumerate(events) if e[:1] == ("scripts",))
    assert sum(e[0] == "worlds" and e[2] == "completed" for e in events[:first_script]) == 4
    before = (tmp_path / "parallel/story.json").read_bytes()
    count = len(provider.calls)
    assert workflow.resume(tmp_path / "parallel") == run
    assert (tmp_path / "parallel/story.json").read_bytes() == before
    assert len(provider.calls) == count


def test_failed_parallel_world_preserves_other_three_checkpoints(tmp_path):
    workflow = StoryWorkflow(ConcurrentStoryProvider(fail=True), max_workers=4)
    with pytest.raises(RuntimeError, match="simulated"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="partial", random_seed=41)
    saved = StoryWorkflow.load(tmp_path / "partial")
    assert saved.status == "failed"
    assert saved.saves[1].world is None
    assert all(saved.saves[i].world_review for i in (0, 2, 3))
    assert all(s.final is None for s in saved.saves)
    restored = StoryWorkflow(MockStoryProvider(), max_workers=4).resume(tmp_path / "partial")
    assert restored.status == "completed"
    assert all(restored.saves[i].world == saved.saves[i].world for i in (0, 2, 3))


def test_presets_and_template_rollback_keep_stories_assets_and_snapshots(store, story):
    draft = store.create("My reel")
    draft.games = games_from_story(story)
    for game in draft.games:
        game.broll_beats = ()
    draft = store.save(draft)
    original = draft.model_copy(deep=True)
    key = "story_narration_prose_candidate/v3.txt"
    draft.prompts[key] += "\nUse plain words.\n"
    draft.story.narration_model = "my-writer"
    draft = store.save(draft)
    preset = store.save_preset(draft, "Simple voice")
    revision = draft.revision
    job = prepare_job(store, draft)
    draft.prompts[key] += "\nAnother edit.\n"
    draft = store.save(draft)
    draft = store.restore_revision(draft, revision, key)
    assert "Another edit" not in draft.prompts[key]
    assert "Use plain words" in draft.prompts[key]
    assert draft.games == original.games
    assert (job.prompts / key).read_text() == draft.prompts[key]
    other = store.create("Other reel")
    applied = store.apply_preset(other, preset["id"])
    assert not applied.games
    assert applied.name == "Other reel"
    assert applied.story.narration_model == "my-writer"
    assert applied.prompts[key] == draft.prompts[key]
    assert applied.preset_revision == preset["id"]
    assert len(store.list_presets()) == 1
    assert len(store.list_revisions(draft.draft_id)) >= 3


@pytest.mark.parametrize("change", ["motion", "model", "force"])
def test_video_change_or_force_reuses_exact_still(store, story, monkeypatch, change):
    draft = store.create()
    draft.games = games_from_story(story)
    for game in draft.games:
        game.broll_beats = ()
    draft = store.save(draft)
    initial = prepare_job(store, draft, "stills", save_number=1)

    def command(self, verb, target, *args):
        branch = RunStore(target)
        run = BrollPipeline.load(branch)
        if "image" not in run.artifacts:
            run.artifacts["image"] = branch.write_bytes("broll_still.png", b"original", "image/png")
            run.image_attempted = True
            run.stages["image_generation"] = StageState(status=StageStatus.COMPLETED)
        if "--image-only" not in args:
            run.artifacts["video"] = branch.write_bytes("broll_video.mp4", b"clip", "video/mp4")
        BrollPipeline._save(branch, run)

    monkeypatch.setattr(ProductionJob, "command", command)
    initial.execute()
    source = initial.draft.games[0].broll_run
    revised = store.load(draft.draft_id)
    if change == "motion":
        revised.games[0].motion.camera_motion = "Move gently forward."
    elif change == "model":
        revised.broll.resolution = "768P"
        revised.broll.video_model = "MiniMax-H3"
    revised = store.save(revised)
    next_job = prepare_job(
        store, revised, "videos", save_number=1, job_id="second", regenerate=change == "force"
    )
    next_job.execute()
    assert next_job.job["status"] == "completed", next_job.job.get("error")
    target = store.runs / next_job.draft.games[0].broll_run
    run = BrollPipeline.load(RunStore(target))
    assert run.reused_still_run_id == source
    assert (target / "broll_still.png").read_bytes() == b"original"
    assert "video" in run.artifacts
    assert next_job.job["progress"]["completed"] == 2
    # A changed still template must not inherit that image.
    revised = store.load(draft.draft_id)
    revised.prompts["broll_still/v2.txt"] += "\nChange the lighting.\n"
    revised = store.save(revised)
    third = prepare_job(store, revised, "stills", save_number=1, job_id="third")
    third.execute()
    assert (
        BrollPipeline.load(
            RunStore(store.runs / third.draft.games[0].broll_run)
        ).reused_still_run_id
        is None
    )


@pytest.mark.parametrize(
    "model,resolution,duration,valid",
    [
        ("MiniMax-H3-Max", "480P", 5, True),
        ("MiniMax-H3-Max", "768P", 15, True),
        ("MiniMax-H3-Max", "2K", 5, False),
        ("MiniMax-H3-Max", "768P", 4, False),
        ("MiniMax-H3", "480P", 5, False),
        ("MiniMax-H3", "2K", 4, True),
    ],
)
def test_minimax_model_capabilities(model, resolution, duration, valid):
    values = dict(video_model=model, resolution=resolution, duration=duration)
    if valid:
        assert BrollSettings(**values).video_model == model
    else:
        with pytest.raises(ValidationError):
            BrollSettings(**values)


def test_corrupt_reused_still_remains_blocked_on_resume(store, story, monkeypatch):
    draft = store.create()
    draft.games = games_from_story(story)
    for game in draft.games:
        game.broll_beats = ()
    draft = store.save(draft)
    initial = prepare_job(store, draft, "stills", save_number=1)

    def save_image(self, verb, target, *args):
        branch = RunStore(target)
        run = BrollPipeline.load(branch)
        run.artifacts["image"] = branch.write_bytes("broll_still.png", b"original", "image/png")
        BrollPipeline._save(branch, run)

    monkeypatch.setattr(ProductionJob, "command", save_image)
    initial.execute()
    reference = initial.draft.games[0].broll_run
    (store.runs / reference / "broll_still.png").write_bytes(b"corrupted")
    draft = store.load(draft.draft_id)
    draft.broll.resolution = "768P"
    draft.broll.video_model = "MiniMax-H3"
    draft = store.save(draft)
    job = prepare_job(store, draft, "videos", save_number=1, job_id="reuse-corrupt")
    calls = []
    monkeypatch.setattr(ProductionJob, "command", lambda *args: calls.append(args))
    for _ in range(2):
        job.execute()
        assert job.job["status"] == "failed"
        assert "checksum" in job.job["error"]
    assert calls == []  # Resume must not replace a corrupt cache with an unplanned paid image.


def test_full_job_exports_real_mp4_and_library_excludes_incomplete_runs(
    store,
    local_assets,
    narration_assets,
    monkeypatch,
):
    from save_reel.providers import elevenlabs_speech
    from save_reel.rendering import probe_media

    _, sources, collection, _ = local_assets
    _, audio, _ = narration_assets
    video = (sources / collection.games[0].broll_run_ids[0] / "broll_video.mp4").read_bytes()
    from test_studio_scripts import FakeWriter

    monkeypatch.setattr("save_reel.story_cli._provider", lambda *args: FakeWriter())
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    speech = FakeSpeech(audio)
    monkeypatch.setattr(elevenlabs_speech, "ElevenLabsSpeechProvider", lambda *args: speech)
    calls = {"cartridges": 0, "stills": 0, "videos": 0}
    barriers = {k: threading.Barrier(4, timeout=10) for k in calls}
    call_lock = threading.Lock()

    class Image:
        def generate(self, prompt, settings):
            key = "cartridges" if settings.image_background == "transparent" else "stills"
            with call_lock:
                calls[key] += 1
            return GeneratedImage(png_cutout((100, 180, 120)))

    class Video:
        def submit(self, image, prompt, settings):
            with call_lock:
                calls["videos"] += 1
            assert calls["stills"] == 8  # All stills completed before any video submission.
            return "test-task"

        def query(self, task_id):
            return VideoTask("succeeded", "https://test.invalid/video.mp4")

        def download(self, url):
            return video

    def command(self, verb, target, *args):
        key = (
            "cartridges"
            if verb == "resume-cartridge"
            else ("stills" if "--image-only" in args else "videos")
        )
        barriers[key].wait()
        if key == "cartridges":
            CartridgePipeline(Image()).execute(RunStore(target))
        else:
            BrollPipeline(Image(), Video()).execute(RunStore(target), image_only=key == "stills")

    monkeypatch.setattr(ProductionJob, "command", command)
    draft = store.create("Complete production test")
    draft.seed = 41
    draft.render = RenderSettings(
        width=216, height=384, intro_seconds=1, countdown_seconds=1, clip_seconds=2,
        polish=ReelPolish(enabled=True),
    )
    draft = store.save(draft)
    job = prepare_job(store, draft, "full")
    job.execute()
    assert job.job["status"] == "completed", job.job.get("error")
    assert calls == {"cartridges": 4, "stills": 8, "videos": 8}
    assert speech.calls == 9
    progress = job.job["progress"]
    assert progress["completed"] == progress["total"]
    output = store.runs / job.job["outputs"]["reel"]
    assert is_finished_reel(output)
    streams = probe_media(output / "reel.mp4")["streams"]
    assert {s["codec_type"] for s in streams} == {"audio", "video"}
    assert (output / "subtitles.srt").exists()
    footage = json.loads((output / "footage_plan.json").read_text())
    assert all([p["index"] for p in parts if p["kind"] == "video"] == [0, 1]
               for parts in footage.values())
    assert [r["run_id"] for r in store.list_runs() if r["finished"]] == [output.name]
    # Repeating the completed job reuses every paid result.
    job.execute()
    assert job.job["status"] == "completed"
    assert calls == {"cartridges": 4, "stills": 8, "videos": 8}
    assert speech.calls == 9
    manifest = output / "narration.json"
    raw = json.loads(manifest.read_text())
    raw["status"] = "failed"
    manifest.write_text(json.dumps(raw))
    assert not any(r["finished"] for r in store.list_runs())
    raw["status"] = "completed"
    manifest.write_text(json.dumps(raw))
    (output / "speech/save_04.mp3").unlink()
    assert not any(r["finished"] for r in store.list_runs())


def test_failed_speech_keeps_parallel_successes_and_never_resubmits_ambiguous_cue(
    narration_assets,
    tmp_path,
):
    from save_reel.narration import NarrationPipeline
    from save_reel.narration_models import SpeechSettings

    source, audio, script = narration_assets
    barrier = threading.Barrier(4, timeout=10)
    lock = threading.Lock()

    class Speech(FakeSpeech):
        entered = 0

        def generate(self, text, settings):
            with lock:
                self.entered += 1
                n = self.entered
            if n <= 4:
                barrier.wait()
            if n == 2:
                raise MediaError("Simulated ambiguous speech timeout")
            return super().generate(text, settings)

    speech = Speech(audio)
    pipeline = NarrationPipeline(speech)
    target = pipeline.prepare(
        source, script, SpeechSettings(voice_id="test"), runs_dir=tmp_path, run_id="parallel-speech"
    )
    with pytest.raises(MediaError, match="ambiguous"):
        pipeline.execute(target, max_workers=4)
    run = pipeline.load(target)
    assert sum(bool(c.audio) for c in run.cues) == 4
    entered = speech.entered
    with pytest.raises(MediaError, match="No duplicate"):
        pipeline.execute(target, max_workers=4)
    assert speech.entered == entered
