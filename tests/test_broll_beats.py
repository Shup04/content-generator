"""Two-beat story, independent paid boundaries, and real offline composition."""

# ruff: noqa: F811 -- shared pytest fixtures
import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError
from test_rendering import local_assets  # noqa: F401
from test_studio import prepare_job, store, story  # noqa: F401

from save_reel.broll import BrollPipeline
from save_reel.cli import build_parser
from save_reel.media_models import BrollSettings
from save_reel.models import StageStatus
from save_reel.polish_models import ReelPolish
from save_reel.providers.media import GeneratedImage, VideoTask
from save_reel.reel_polish import plan_footage
from save_reel.render_models import RenderSettings
from save_reel.rendering import ReelRenderer
from save_reel.storage import RunStore
from save_reel.story_export import export_story
from save_reel.story_models import EnvironmentVariables
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_simulation import check_world
from save_reel.studio_models import JobRequest, StudioDraft
from save_reel.studio_store import compile_game, games_from_story, snapshot_prompts
from save_reel.studio_worker import ProductionJob


def test_two_story_beats_compile_and_export_with_shared_style(story, store, tmp_path):
    draft = store.create()
    draft.games = games_from_story(story)
    snapshot_prompts(draft, tmp_path / "prompts")
    assert "broll_beats" in story.templates
    assert story.templates["broll_beats"].template.version == "v1"
    exported = RunStore.create(tmp_path / "exports", story.run_id)
    reel = export_story(story, exported)
    for save in reel.saves:
        for number in (1, 2):
            assert f"broll_{number:02}_still_prompt" in save.artifacts
            assert f"broll_{number:02}_video_prompt" in save.artifacts
            assert (exported.run_dir / save.artifacts[f"broll_{number:02}_values"].path).is_file()
    for slot, game in zip(story.saves, draft.games, strict=True):
        beats = slot.final.environment.broll_beats
        assert len(beats) == 2
        assert [b.type for b in beats] == ["establishing", "detail"]
        assert beats[0].description != beats[1].description
        assert beats[0].shot_composition != beats[1].shot_composition
        first, second = [game.clip_values(i)[0] for i in (1, 2)]
        assert (first.colour_palette, first.mood, first.key_surfaces) == (
            second.colour_palette, second.mood, second.key_surfaces,
        )
        prompts = [compile_game(draft, game, tmp_path / "prompts", i) for i in (1, 2)]
        assert prompts[0]["still"].text != prompts[1]["still"].text
        assert prompts[0]["video"].text != prompts[1]["video"].text
        for beat, compiled in zip(beats, prompts, strict=True):
            assert beat.description in compiled["still"].text
            assert beat.camera_motion in compiled["video"].text


@pytest.mark.parametrize("field", ["description", "shot_composition"])
def test_identical_or_cosmetically_changed_beats_are_rejected(story, field):
    env = story.saves[0].final.environment.model_dump()
    env["broll_beats"][1][field] = env["broll_beats"][0][field].upper() + "!"
    with pytest.raises(ValidationError, match="distinct"):
        EnvironmentVariables.model_validate(env)


def test_exactly_two_beats_required_but_legacy_missing_is_allowed(story):
    env = story.saves[0].final.environment.model_dump()
    for count in (0, 1, 3):
        broken = {**env, "broll_beats": (env["broll_beats"] * 2)[:count]}
        with pytest.raises(ValidationError):
            EnvironmentVariables.model_validate(broken)
    env.pop("broll_beats")
    assert EnvironmentVariables.model_validate(env).broll_beats is None
    slot = story.saves[0]
    world = slot.world.model_copy(deep=True)
    world.environment.broll_beats = None
    with pytest.raises(ValueError, match="two distinct broll_beats"):
        check_world(slot, world, require_beats=True)


def test_defaults_and_cli_share_provider_configuration():
    draft = StudioDraft(draft_id="defaults")
    assert (draft.broll.video_model, draft.broll.resolution, draft.broll.duration) == (
        "MiniMax-H3-Max", "480P", 5,
    )
    assert draft.broll.clips_per_save == 2
    assert draft.render.polish.max_slowdown == 1.5
    assert draft.render.polish.allow_still_fallback
    args = build_parser().parse_args([
        "generate-broll", "--values", "world.json", "--motion-values", "motion.json",
    ])
    assert (args.video_model, args.resolution, args.duration) == (
        draft.broll.video_model, draft.broll.resolution, draft.broll.duration,
    )
    assert BrollSettings(video_model="MiniMax-H3", resolution="768P").resolution == "768P"
    assert ReelPolish(max_slowdown=2).max_slowdown == 2  # Explicit legacy opt-in.


@pytest.mark.parametrize("target,slowdown,still", [(9, 1, 0), (12, 1.2, 0), (20, 1.5, 120)])
def test_two_clip_plan_uses_both_once_and_respects_slowdown(target, slowdown, still):
    plan = plan_footage([5, 5], target, 24, 1.5)
    videos = [p for p in plan if p["kind"] == "video"]
    assert [p["index"] for p in videos] == [0, 1]
    assert all(p["slowdown"] == slowdown for p in videos)
    assert sum(p["frames"] for p in plan) == target * 24
    assert sum(p["frames"] for p in plan if p["kind"] == "still") == still
    if target == 9:
        assert [p["frames"] for p in videos] == [108, 108]
    with pytest.raises(ValueError, match="will not loop"):
        plan_footage([5, 5], 20, 24, 1.5, allow_still_fallback=False)


def test_independent_media_cache_and_selective_regeneration(store, story, monkeypatch):
    draft = store.create()
    draft.games = games_from_story(story)
    draft.parallel_saves = 1
    draft = store.save(draft)
    calls = {"image": 0, "video": 0}

    class Image:
        def generate(self, prompt, settings):
            calls["image"] += 1
            return GeneratedImage(f"still-{calls['image']}".encode())

    class Video:
        def submit(self, image, prompt, settings):
            calls["video"] += 1
            assert settings.video_model == "MiniMax-H3-Max"
            assert settings.resolution == "480P" and settings.duration == 5
            return f"task-{calls['video']}"

        def query(self, task_id):
            return VideoTask("succeeded", "https://test.invalid/" + task_id)

        def download(self, url):
            return url.encode()

    def command(self, verb, target, *args):
        BrollPipeline(Image(), Video()).execute(RunStore(target), image_only="--image-only" in args)

    monkeypatch.setattr(ProductionJob, "command", command)
    first = prepare_job(store, draft, "videos")
    first.execute()
    assert first.job["status"] == "completed", first.job.get("error")
    assert calls == {"image": 8, "video": 8}
    assert first.job["progress"]["completed"] == first.job["progress"]["total"] == 16
    original = store.load(draft.draft_id)
    refs = [g.clip_run(i) for g in original.games for i in (1, 2)]
    original_bytes = [(store.runs / ref / "broll.json").read_bytes() for ref in refs]
    for game in original.games:
        assert game.clip_run(1) != game.clip_run(2)
        for i in (1, 2):
            folder = store.runs / game.clip_run(i)
            assert (folder / f"broll_{i:02}_still.png").is_file()
            assert (folder / f"broll_{i:02}.mp4").is_file()
            BrollPipeline(None, None).execute(RunStore(folder))
    cached = prepare_job(store, original, "videos", job_id="cached")
    cached.execute()
    assert cached.job["status"] == "completed"
    assert calls == {"image": 8, "video": 8}
    second = prepare_job(store, store.load(draft.draft_id), "videos", job_id="second-beat",
                         regenerate=True, save_number=2, beat_number=2)
    second.execute()
    assert second.job["status"] == "completed", second.job.get("error")
    assert calls == {"image": 8, "video": 9}  # New animation reuses its exact source still.
    revised = store.load(draft.draft_id)
    changed = [g.clip_run(i) for g in revised.games for i in (1, 2)]
    assert [i for i, pair in enumerate(zip(refs, changed)) if pair[0] != pair[1]] == [3]
    run = BrollPipeline.load(RunStore(store.runs / changed[3]))
    assert run.reused_still_run_id == refs[3]
    # The previously accepted media and task IDs remain independently usable.
    for ref, before in zip(refs, original_bytes, strict=True):
        assert json.loads((store.runs / ref / "broll.json").read_bytes())["video_task_id"] == (
            json.loads(before)["video_task_id"]
        )
    both = prepare_job(store, revised, "stills", job_id="both-beats",
                       regenerate=True, save_number=3)
    both.execute()
    assert both.job["status"] == "completed"
    assert calls == {"image": 10, "video": 9}
    again = store.load(draft.draft_id)
    assert again.games[2].clip_run(1) != revised.games[2].clip_run(1)
    assert again.games[2].clip_run(2) != revised.games[2].clip_run(2)
    assert again.games[1] == revised.games[1]


def test_beat_selector_does_not_affect_cartridges():
    with pytest.raises(ValidationError, match="beat selector"):
        JobRequest(draft_id="test", revision=0, action="cartridges", beat_number=2)


@pytest.mark.parametrize("duration", [1, 4])
def test_real_ffmpeg_uses_two_distinct_clips_and_optional_still(local_assets, tmp_path, duration):
    _, original, collection, colors = local_assets
    sources = tmp_path / "sources"
    shutil.copytree(original, sources)
    collection = collection.model_copy(deep=True)
    for game in collection.games:
        source = RunStore(sources / game.broll_run_ids[0])
        saved = BrollPipeline.load(source)
        second = BrollPipeline(None, None).prepare(
            saved.values, saved.motion, saved.settings, runs_dir=sources,
            run_id=game.broll_run_ids[0] + "-detail", beat_number=2,
        )
        # A distinct blue source makes the actual cut observable, not just a graph assertion.
        out = second.run_dir / "broll_02.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=216x384:r=24:d=1", "-c:v", "libx264", "-threads", "1",
                        "-pix_fmt", "yuv420p", str(out)], check=True, capture_output=True)
        run = BrollPipeline.load(second)
        run.artifacts["video"] = ReelRenderer._record(second, out.name)
        run.status = StageStatus.COMPLETED
        BrollPipeline._save(second, run)
        game.broll_run_ids = (*game.broll_run_ids, second.run_dir.name)
    settings = RenderSettings(width=216, height=384, intro_seconds=1, countdown_seconds=1,
                              clip_seconds=duration, show_clip_titles=False,
                              polish=ReelPolish(enabled=True, transition_seconds=0))
    output = ReelRenderer().render(collection, settings, source_runs_dir=sources,
                                   runs_dir=tmp_path, run_id="two-clips")
    manifest = json.loads((output.parent / "render.json").read_text())
    world = next(s for s in manifest["timeline"] if s["kind"] == "broll")
    assert len(world["source_run_ids"]) == 2
    plan = json.loads((output.parent / "footage_01.json").read_text())
    clips = [p for p in plan if p["kind"] == "video"]
    assert [p["index"] for p in clips] == [0, 1]
    assert all(p["slowdown"] <= 1.5 for p in clips)
    assert (plan[-1]["kind"] == "still") == (duration == 4)
    offset = world["start"]
    for i, clip in enumerate(clips):
        sample_at = offset + clip["frames"] / settings.fps / 2
        pixel = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", str(sample_at), "-i", str(output),
            "-vf", "crop=2:2:100:100,scale=1:1", "-frames:v", "1", "-pix_fmt", "rgb24",
            "-f", "rawvideo", "-",
        ], check=True, capture_output=True).stdout
        expected = colors[0] if i == 0 else (0, 0, 255)
        assert all(abs(a - b) < 15 for a, b in zip(pixel, expected, strict=True))
        offset += clip["frames"] / settings.fps
    assert all("-stream_loop" not in p.read_text() for p in output.parent.glob("command_*.json"))


def test_old_story_without_beats_loads_and_keeps_one_clip(story, tmp_path):
    raw = story.model_dump(mode="json")
    raw["settings"].pop("broll_prompt_version")
    raw["templates"].pop("broll_beats")
    for slot in raw["saves"]:
        slot["world"]["environment"].pop("broll_beats")
        slot["final"]["environment"].pop("broll_beats")
    folder = tmp_path / story.run_id
    folder.mkdir()
    (folder / "story.json").write_text(json.dumps(raw))
    restored = StoryWorkflow.load(folder)
    assert restored.settings.broll_prompt_version is None
    assert all(list(g.clip_numbers()) == [1] for g in games_from_story(restored))
