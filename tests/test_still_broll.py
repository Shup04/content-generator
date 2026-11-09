"""Intentional still shots: paid boundaries, cached transitions and real composition."""

# ruff: noqa: F811 -- shared pytest fixtures
import io
import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError
from test_narration import alignment
from test_rendering import local_assets  # noqa: F401
from test_studio import prepare_job, store, story  # noqa: F401

from save_reel.broll import BrollPipeline
from save_reel.media_models import BrollSettings
from save_reel.models import StageStatus
from save_reel.narration import NarrationPipeline
from save_reel.narration_models import NarrationScript, SpeechMetadata, SpeechSettings
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.polish_models import ReelPolish
from save_reel.providers.media import GeneratedImage, VideoTask
from save_reel.providers.speech import GeneratedSpeech
from save_reel.reel_polish import plan_footage
from save_reel.render_models import RenderGame, RenderSettings
from save_reel.rendering import ReelRenderer, probe_media
from save_reel.storage import RunStore
from save_reel.story_tiers import TIERS
from save_reel.studio_models import StudioDraft
from save_reel.studio_store import games_from_story, is_finished_reel
from save_reel.studio_worker import ProductionJob


@pytest.mark.parametrize("mode,video_count", [("mixed", 4), ("stills", 0)])
def test_full_job_only_animates_selected_shots_and_reuses_media(
    store, story, monkeypatch, mode, video_count,
):
    draft = store.create()
    draft.games = games_from_story(story)
    draft.broll = BrollSettings(visual_mode=mode, clips_per_save=2,
                                stills_per_save=1 if mode == "mixed" else 2)
    draft.parallel_saves = 1
    draft = store.save(draft)
    calls = {"image": 0, "video": 0}

    class Image:
        def generate(self, prompt, settings):
            calls["image"] += 1
            return GeneratedImage(f"image-{calls['image']}".encode())

    class Video:
        def submit(self, image, prompt, settings):
            calls["video"] += 1
            return f"task-{calls['video']}"

        def query(self, task_id):
            return VideoTask("succeeded", "https://test.invalid/" + task_id)

        def download(self, url):
            return url.encode()

    def command(self, verb, target, *args):
        assert verb == "resume-broll"
        BrollPipeline(Image(), Video()).execute(RunStore(target), image_only="--image-only" in args)

    media = ProductionJob.media
    monkeypatch.setattr(ProductionJob, "media", lambda self, kind:
                        media(self, kind) if kind != "cartridges" else None)
    monkeypatch.setattr(ProductionJob, "command", command)
    monkeypatch.setattr(ProductionJob, "scripts", lambda self, **kw: None)
    monkeypatch.setattr(ProductionJob, "validate_scripts", lambda self: None)
    monkeypatch.setattr(ProductionJob, "render", lambda self: None)
    monkeypatch.setattr(ProductionJob, "narrate", lambda self:
                        self.job["outputs"].update(reel="offline-reel"))
    monkeypatch.setattr("save_reel.studio_store.is_finished_reel", lambda path: True)

    first = prepare_job(store, draft, "full")
    first.execute()
    assert first.job["status"] == "completed", first.job.get("error")
    assert calls == {"image": 8, "video": video_count}
    video_stages = [s for s in first.job["progress"]["stages"] if s["id"] == "videos"]
    assert sum(len(s["units"]) for s in video_stages) == video_count
    assert all(state == "completed" for s in video_stages for state in s["units"].values())
    saved = store.load(draft.draft_id)
    refs = [g.clip_run(i) for g in saved.games for i in (1, 2)]
    for game in saved.games:
        for beat in (1, 2):
            run = BrollPipeline.load(RunStore(store.runs / game.clip_run(beat)))
            assert ("video" in run.artifacts) == (saved.broll.media_kind(beat) == "video")

    cached = prepare_job(store, saved, "full", job_id="cached")
    cached.execute()
    assert cached.job["status"] == "completed", cached.job.get("error")
    assert calls == {"image": 8, "video": video_count}

    rejected = prepare_job(store, store.load(draft.draft_id), "videos", job_id="still-selected",
                           beat_number=2, regenerate=True)
    rejected.execute()
    assert rejected.job["status"] == "failed"
    assert "uses still images" in rejected.job["error"]
    assert calls == {"image": 8, "video": video_count}

    saved = store.load(draft.draft_id)
    saved.broll.visual_mode = "videos"
    saved = store.save(saved)
    upgraded = prepare_job(store, saved, "full", job_id="all-videos")
    upgraded.execute()
    assert upgraded.job["status"] == "completed", upgraded.job.get("error")
    assert calls == {"image": 8, "video": 8}
    saved = store.load(draft.draft_id)
    assert [g.clip_run(i) for g in saved.games for i in (1, 2)] == refs

    saved.broll.visual_mode = mode
    saved = store.save(saved)
    restored = prepare_job(store, saved, "full", job_id="restore-mode")
    restored.execute()
    assert restored.job["status"] == "completed", restored.job.get("error")
    assert calls == {"image": 8, "video": 8}


@pytest.mark.parametrize("durations,target,expected", [
    ([5, None], 20, [("video", 180), ("still", 300)]),
    ([5, None], 6, [("video", 72), ("still", 72)]),
    ([None, None], 10, [("still", 120), ("still", 120)]),
    ([None], 1, [("still", 24)]),
])
def test_intentional_stills_cover_speech_even_without_fallback(durations, target, expected):
    plan = plan_footage(durations, target, 24, 1.5, allow_still_fallback=False)
    assert [(p["kind"], p["frames"]) for p in plan] == expected
    assert all(1 <= p["slowdown"] <= 1.5 for p in plan)
    assert sum(p["frames"] for p in plan) == target * 24


def test_modes_validate_and_legacy_drafts_keep_videos(story):
    assert StudioDraft(draft_id="new").broll.visual_mode == "mixed"
    assert BrollSettings.model_validate({"clips_per_save": 2}).visual_mode == "videos"
    assert BrollSettings(visual_mode="mixed", clips_per_save=1).shots_per_save == 2
    for mode in ("mixed", "stills"):
        for count in (1, 2, 3):
            settings = BrollSettings(visual_mode=mode, stills_per_save=count)
            assert settings.shots_per_save == count + (mode == "mixed")
        with pytest.raises(ValidationError):
            BrollSettings(visual_mode=mode, stills_per_save=4)
    for count in (1, 2):
        settings = BrollSettings.model_validate({"visual_mode": "stills", "clips_per_save": count})
        assert settings.stills_per_save == settings.shots_per_save == count
    games = games_from_story(story)
    for game in games:
        game.broll_beats = ()
    assert StudioDraft(draft_id="old", games=games, broll=BrollSettings(visual_mode="mixed"))
    for mode in ("videos", "stills"):
        restored = StudioDraft.model_validate({
            "draft_id": "legacy-single-view", "games": [g.model_dump() for g in games],
            "broll": {"visual_mode": mode, "clips_per_save": 2},
        })
        assert restored.broll.shots_per_save == 1
    with pytest.raises(ValidationError, match="one media kind"):
        RenderGame(title="World", values_file="values.json", cartridge_run_id="cart",
                   broll_run_ids=("one", "two"), broll_kinds=("still",))


@pytest.mark.parametrize("mode,polished,count", [
    ("mixed", True, 2), ("mixed", True, 4), ("stills", True, 3), ("stills", False, 3),
])
def test_stills_render_and_survive_narration_without_source_videos(
    local_assets, tmp_path, mode, polished, count,
):
    Image = pytest.importorskip("PIL.Image")
    _, original, collection, colors = local_assets
    sources = tmp_path / "sources"
    shutil.copytree(original, sources)
    collection = collection.model_copy(deep=True)

    class Still:
        def __init__(self, color):
            self.color = color

        def generate(self, prompt, settings):
            data = io.BytesIO()
            Image.new("RGB", (216, 384), self.color).save(data, format="PNG")
            return GeneratedImage(data.getvalue())

    for number, game in enumerate(collection.games):
        original_id = game.broll_run_ids[0]
        saved = BrollPipeline.load(RunStore(sources / original_id))
        refs, kinds = [], []
        for beat in range(1, count + 1):
            kind = "video" if mode == "mixed" and beat == 1 else "still"
            kinds.append(kind)
            if kind == "video":
                refs.append(original_id)
                continue
            palette = (colors[number], (0, 0, 255), (0, 255, 0), (255, 255, 0))
            pipeline = BrollPipeline(Still(palette[beat - 1]), None)
            store = pipeline.prepare(saved.values, saved.motion, saved.settings,
                                     runs_dir=sources, run_id=f"still-{number}-{beat}",
                                     beat_number=beat)
            image_run = pipeline.execute(store, image_only=True)
            assert image_run.status == StageStatus.PENDING
            assert "video" not in image_run.artifacts
            refs.append(store.run_dir.name)
        game.broll_run_ids = tuple(refs)
        game.broll_kinds = tuple(kinds)

    settings = RenderSettings(width=216, height=384, intro_seconds=1, countdown_seconds=1,
                              clip_seconds=4 if polished else 1, show_clip_titles=False,
                              polish=ReelPolish(enabled=polished, allow_still_fallback=False,
                                               still_position="before", transition_seconds=0))
    output = ReelRenderer().render(collection, settings, source_runs_dir=sources,
                                   runs_dir=tmp_path, run_id="base")
    manifest = json.loads((output.parent / "render.json").read_text())
    assert manifest["collection"]["games"][0]["broll_kinds"] == list(
        collection.games[0].broll_kinds
    )
    assert not (output.parent / "inputs/save_01/broll_02.mp4").exists()
    if mode == "stills":
        assert not list((output.parent / "inputs").rglob("*.mp4"))
    segments = [s for s in manifest["timeline"] if s["kind"] == "broll" and s["save_number"] == 1]
    if polished:
        plan = json.loads((output.parent / "footage_01.json").read_text())
        assert [p["kind"] for p in plan] == list(collection.games[0].broll_kinds)
        offset = segments[0]["start"]
        samples = []
        for part in plan:
            samples.append(offset + part["frames"] / settings.fps / 2)
            offset += part["frames"] / settings.fps
    else:
        samples = [s["start"] + s["duration"] / 2 for s in segments]
    for at, color in zip(samples, (colors[0], (0, 0, 255), (0, 255, 0), (255, 255, 0))[:count],
                         strict=True):
        pixel = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(output),
            "-vf", "crop=2:2:100:100,scale=1:1", "-frames:v", "1", "-pix_fmt", "rgb24",
            "-f", "rawvideo", "-",
        ], check=True, capture_output=True).stdout
        assert len(pixel) == 3
        assert all(abs(a - b) < 12 for a, b in zip(pixel, color, strict=True))

    if not polished:
        return
    tone = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3.2", "-c:a", "libmp3lame", str(tone)],
                   check=True, capture_output=True)

    class Speech:
        def generate(self, text, settings):
            duration = 1.2 if text == INTRO_SCRIPT else 0.6 if text.startswith("GAME") else 3
            return GeneratedSpeech(tone.read_bytes(), SpeechMetadata(
                alignment=alignment(text, duration)
            ))

    script = NarrationScript(intro=INTRO_SCRIPT, games=[
        dict(title=g.title, text="You gather supplies and return before dark.",
             survivability_tier=tier)
        for g, tier in zip(collection.games, TIERS, strict=True)
    ])
    pipeline = NarrationPipeline(Speech())
    narration_store = pipeline.prepare(output.parent, script, SpeechSettings(voice_id="offline"),
                                       runs_dir=tmp_path, run_id="narrated")
    result = pipeline.execute(narration_store)
    assert is_finished_reel(narration_store.run_dir)
    assert {s["codec_type"] for s in probe_media(result)["streams"]} == {"audio", "video"}
    plans = json.loads((narration_store.run_dir / "footage_plan.json").read_text())
    assert all([p["kind"] for p in parts] == list(collection.games[0].broll_kinds)
               for parts in plans.values())
    assert all(sum(p["frames"] for p in parts) > 0 for parts in plans.values())
    assert not (narration_store.run_dir / "inputs/save_01/broll_02.mp4").exists()
