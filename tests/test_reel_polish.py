"""Polished timing and real local FFmpeg assembly, with offline speech/model fakes."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import subprocess
from array import array

import pytest
from test_narration import alignment
from test_rendering import local_assets  # noqa: F401

from save_reel.models import ConceptRequest
from save_reel.narration import NarrationPipeline
from save_reel.narration_models import NarrationScript, SpeechMetadata, SpeechSettings
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.polish_models import ReelPolish
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.providers.speech import GeneratedSpeech
from save_reel.reel_polish import plan_footage
from save_reel.render_models import RenderSettings
from save_reel.rendering import ReelRenderer, probe_media
from save_reel.story_models import StorySettings
from save_reel.story_novelty import reel_review_passes
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_seeds import make_seeds
from save_reel.story_tiers import TIER_INTROS, TIERS
from save_reel.studio_store import is_finished_reel


@pytest.mark.parametrize("durations,target,slow,expected_still", [
    ([10], 15, 2, 0), ([10], 25, 2, 120), ([4, 6], 25, 2, 120), ([10], 6, 2, 0),
    ([10], 25, 1.5, 240),
])
def test_footage_budget_never_loops(durations, target, slow, expected_still):
    plan = plan_footage(durations, target, 24, slow)
    assert sum(p["frames"] for p in plan) == target * 24
    motion = [p for p in plan if p["kind"] == "video"]
    assert len({p["index"] for p in motion}) == len(motion)
    assert all(1 <= p["slowdown"] <= slow for p in motion)
    assert sum(p["frames"] for p in plan if p["kind"] == "still") == expected_still


def test_tiers_are_shuffled_separately_from_slots_and_preserved_when_regenerating():
    settings = StorySettings()
    assignments = []
    for seed in range(12):
        seeds, _ = make_seeds(seed, settings, ())
        assert {s.survivability_tier for s in seeds} == set(TIERS)
        assignments.append(tuple(s.survivability_tier for s in seeds))
        replacement, _ = make_seeds(seed + 100, settings, (), fixed=seeds[1:])
        assert replacement[0].survivability_tier == seeds[0].survivability_tier
    assert len(set(assignments)) > 4
    assert len({s[0] for s in assignments}) > 1


def test_world_tier_contract_and_prompt_snapshot(tmp_path):
    class Spy(MockStoryProvider):
        calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return super().generate(**kwargs)

    provider = Spy()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="tiered", random_seed=41)
    assert {s.final.brief.survivability_tier for s in run.saves} == set(TIERS)
    assert "tiers" in run.templates
    for call in provider.calls:
        if call["stage"] in ("candidates", "review", "reel_review", "world_review"):
            assert "SURVIVABILITY TIERS" in call["prompt"]
        if call["stage"] == "narration_prose_candidate":
            assert "SURVIVABILITY TIERS" not in call["prompt"]
    poor_spread = run.reel_review.model_copy(update={"outcome_spread": 0.2})
    assert not reel_review_passes(poor_spread, run.settings.novelty)
    slot = run.saves[0]
    bad_set = slot.candidates.model_copy(deep=True)
    bad_set.candidates[0].survivability_tier = None
    with pytest.raises(ValueError, match="assigned survivability_tier"):
        workflow._check_candidates(run, slot, bad_set)
    manifest = json.loads((tmp_path / "tiered/manifest.json").read_text())
    assert {s["survivability_tier"] for s in manifest["saves"]} == set(TIERS)


def test_polish_is_explicit_for_old_settings_and_default_for_new_studio_drafts():
    from save_reel.studio_models import StudioDraft

    assert not RenderSettings.model_validate({"clip_seconds": 23}).polish.enabled
    assert StudioDraft(draft_id="new").render.polish.enabled
    assert not StudioDraft.model_validate({"draft_id": "old", "render": {
        "clip_seconds": 23, "loop_short_clips": True,
    }}).render.polish.enabled


@pytest.mark.parametrize("placement", ["before", "after"])
def test_real_polished_reel_speech_timing_still_fallback_and_resume(
    local_assets, tmp_path, placement,
):
    _, sources, collection, _ = local_assets
    settings = RenderSettings(width=216, height=384, intro_seconds=1, countdown_seconds=1,
                              clip_seconds=3, polish=ReelPolish(
                                  enabled=True, still_position=placement,
                              ))
    base = ReelRenderer().render(collection, settings, source_runs_dir=sources,
                                 runs_dir=tmp_path, run_id="base")
    # Synthetic inputs have no still image: the base renderer must extract one locally.
    assert (base.parent / "inputs/save_01/broll_01_still.png").is_file()
    tone = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3.2", "-c:a", "libmp3lame", str(tone)],
                   check=True, capture_output=True)

    class Speech:
        calls = []

        def generate(self, text, settings):
            self.calls.append(text)
            duration = 1.2 if text == INTRO_SCRIPT else 0.6 if text.startswith("GAME") else 3
            return GeneratedSpeech(tone.read_bytes(), SpeechMetadata(
                alignment=alignment(text, duration)
            ))

    script = NarrationScript(intro=INTRO_SCRIPT, games=[
        dict(title=g.title, text="You gather supplies and return before dark.",
             survivability_tier=tier)
        for g, tier in zip(collection.games, TIERS, strict=True)
    ])
    provider = Speech()
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(base.parent, script, SpeechSettings(voice_id="offline"),
                             runs_dir=tmp_path, run_id="polished")
    result = pipeline.execute(store, max_workers=2)
    run = pipeline.load(store)
    assert len(provider.calls) == 9
    assert is_finished_reel(store.run_dir)
    plans = json.loads((store.run_dir / "speech_plan.json").read_text())
    for number in range(1, 5):
        title = next(s for s in run.timeline if s.kind == "save_intro" and s.save_number == number)
        world = next(s for s in run.timeline if s.kind == "broll" and s.save_number == number)
        spoken = next(p for p in plans if p["cue_id"] == f"title_{number:02}")
        assert 0.8 <= title.duration <= 1.5
        assert title.start <= spoken["words"][0]["start"]
        assert spoken["words"][-1]["end"] <= world.start
        cue = next(c for c in run.cues if c.cue_id == f"save_{number:02}")
        assert cue.text.startswith(TIER_INTROS[TIERS[number - 1]])
    footage = json.loads((store.run_dir / "footage_plan.json").read_text())
    order = ["video", "still"] if placement == "after" else ["still", "video"]
    assert all([p["kind"] for p in parts] == order for parts in footage.values())
    assert all(
        next(p for p in parts if p["kind"] == "video")["slowdown"] == settings.polish.max_slowdown
        for parts in footage.values()
    )
    assert "ui/system.ttf" in (store.run_dir / "mix_filter.txt").read_text()
    assert "#79d9e8" in (store.run_dir / "mix_filter.txt").read_text()
    for command in store.run_dir.glob("command_*.json"):
        assert "-stream_loop" not in command.read_text()
    info = probe_media(result)
    assert {s["codec_type"] for s in info["streams"]} == {"audio", "video"}
    # An AAC stream can exist while all delayed speech is silent. Decode the final
    # mux, checking the tone in every spoken window (intro, four titles, four saves).
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(result), "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "-",
    ], check=True, capture_output=True).stdout
    samples = array("h", decoded)
    for plan in plans:
        midpoint = plan["offset"] + (plan["trim_end"] - plan["trim_start"]) / plan["tempo"] / 2
        window = samples[round((midpoint - 0.1) * 8000):round((midpoint + 0.1) * 8000)]
        rms = (sum(sample ** 2 for sample in window) / len(window)) ** 0.5 / 32768
        assert rms > 0.01, f"{plan['cue_id']} is silent in the finished reel"
    assert NarrationPipeline(None).execute(store) == result
    assert len(provider.calls) == 9
