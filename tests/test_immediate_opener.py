"""Cartridge visibility and countdown timing are independent of the spoken intro."""

import wave
from pathlib import Path

import pytest
from test_narration import FakeSpeech, narration_assets  # noqa: F401
from test_rendering import frame, local_assets, pixel  # noqa: F401

from save_reel.narration import NarrationPipeline
from save_reel.narration_models import NarrationGame, NarrationScript, SpeechSettings
from save_reel.opener import timed_intro_captions
from save_reel.opener_style import HOOK_CAPTIONS, HOOK_SCRIPT, INTRO_SCRIPT, OpenerStyle
from save_reel.render_models import RenderCollection, RenderSettings, build_timeline
from save_reel.rendering import ReelRenderer, probe_media, write_countdown_audio
from save_reel.studio_models import StudioDraft


@pytest.mark.parametrize("spoken,selection,expected", [(2, 3, 3), (4, 3, 4)])
def test_selection_overlaps_intro_and_waits_for_long_speech(spoken, selection, expected):
    collection = RenderCollection.model_validate_json(
        Path("examples/cartridges/v2/collection.json").read_text()
    )
    settings = RenderSettings(intro_seconds=spoken, countdown_seconds=selection,
                              opener=OpenerStyle(immediate_selection=True))
    timeline = build_timeline(collection, settings)
    assert timeline[0].start == timeline[1].start == 0
    assert timeline[2].start == expected
    assert settings.opener_seconds == expected


def test_new_drafts_explain_reincarnation_while_preserving_saved_wording():
    draft = StudioDraft(draft_id="new")
    assert draft.render.opener.intro_script == INTRO_SCRIPT
    assert draft.render.opener.immediate_selection
    assert OpenerStyle.model_validate({"immediate_selection": True}).intro_script == HOOK_SCRIPT
    assert draft.render.selection_start == 0
    assert draft.render.countdown_start == 3
    assert draft.render.opener.countdown_after_intro
    assert draft.render.opener_seconds == 6
    assert not OpenerStyle.model_validate({"immediate_selection": True}).countdown_after_intro
    old = StudioDraft.model_validate({"draft_id": "old", "render": {
        "intro_seconds": 4, "countdown_seconds": 5,
    }})
    assert old.render.opener.intro_script == INTRO_SCRIPT
    assert old.render.opener_seconds == 9


@pytest.mark.parametrize("spoken", [2, 4])
def test_countdown_digits_and_beeps_wait_for_intro_but_cartridges_do_not(tmp_path, spoken):
    from save_reel.opener import opener_filter

    settings = RenderSettings(intro_seconds=spoken, countdown_seconds=3,
                              opener=OpenerStyle(immediate_selection=True,
                                                 countdown_after_intro=True))
    assert settings.selection_start == 0
    assert settings.countdown_start == spoken
    assert settings.opener_seconds == spoken + 3
    graph = opener_filter(settings)
    assert "enable='gte(t,0)'" in graph  # Cartridges are already visible.
    for index, digit in enumerate((3, 2, 1)):
        line = next(part for part in graph.split(",drawtext=") if f"count_{digit}.txt" in part)
        start = float(spoken + index)
        assert f"gte(t,{start})*lt(t,{start + 1})" in line
    path = tmp_path / "countdown.wav"
    write_countdown_audio(path, settings, settings.opener_seconds)
    with wave.open(str(path), "rb") as audio:
        rate = audio.getframerate()
        assert not any(audio.readframes(spoken * rate))
        for index in range(3):
            audio.setpos((spoken + index) * rate)
            assert any(audio.readframes(rate // 5))


def test_hook_captions_follow_their_own_words_and_reject_mismatched_speech():
    words = [{"text": word, "start": n * 0.3, "end": (n + 1) * 0.3}
             for n, word in enumerate(HOOK_SCRIPT.split())]
    captions = timed_intro_captions(words, HOOK_CAPTIONS)
    assert [c["text"] for c in captions] == list(HOOK_CAPTIONS)
    assert captions[0]["end"] == captions[1]["start"] == 0.6
    with pytest.raises(ValueError, match="configured intro"):
        timed_intro_captions(words)


@pytest.mark.parametrize("wording,after_intro", [
    ("short", False), ("reincarnation", False), ("reincarnation", True),
])
def test_unpolished_hook_retimes_the_reveal_without_repeating_old_intro(
    local_assets, narration_assets, tmp_path, wording, after_intro,  # noqa: F811
):
    _, sources, collection, colours = local_assets
    _, audio, _ = narration_assets
    settings = RenderSettings(width=216, height=384, intro_seconds=4, countdown_seconds=3,
                              clip_seconds=0.5, opener=OpenerStyle(immediate_selection=True,
                                  intro_wording=wording, countdown_after_intro=after_intro))
    base = ReelRenderer().render(collection, settings, source_runs_dir=sources,
                                runs_dir=tmp_path, run_id="base")
    script = NarrationScript(intro=settings.opener.intro_script, games=[
        NarrationGame(title=game.title, text="Stay here.") for game in collection.games
    ])
    pipeline = NarrationPipeline(FakeSpeech(audio))
    store = pipeline.prepare(base.parent, script, SpeechSettings(voice_id="offline"),
                             runs_dir=tmp_path, run_id="spoken")
    output = pipeline.execute(store)
    run = pipeline.load(store)
    countdown_start = run.effective_intro_seconds if after_intro else 0
    world_start = countdown_start + 3
    assert run.timeline[1].start == countdown_start
    assert run.timeline[2].start == run.cues[1].start == world_start
    assert float(probe_media(output)["format"]["duration"]) == pytest.approx(
        world_start + 2, abs=1 / 24,
    )
    assert pixel(frame(output, 0), 55, 160) == pytest.approx(colours[0], abs=8)
    for index, rgb in enumerate(colours):
        assert pixel(frame(output, world_start + .25 + index * .5), 108, 192) == pytest.approx(
            rgb, abs=8,
        )
