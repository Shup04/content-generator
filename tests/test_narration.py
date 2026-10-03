import base64
import hashlib
import importlib.util
import json
import subprocess
from array import array

import pytest
from pydantic import ValidationError
from test_rendering import local_assets  # noqa: F401 -- shared synthetic media fixture

from save_reel.cli import main
from save_reel.models import StageStatus
from save_reel.narration import NarrationPipeline, speech_plan, srt_text
from save_reel.narration_models import (
    NarrationCue,
    NarrationScript,
    SpeechAlignment,
    SpeechMetadata,
    SpeechSettings,
)
from save_reel.opener import timed_intro_captions
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.providers.media import MediaError
from save_reel.providers.speech import GeneratedSpeech
from save_reel.render_models import RenderSettings
from save_reel.rendering import ReelRenderer, probe_media


def alignment(text="Hello world.", duration=0.3):
    return SpeechAlignment(
        characters=tuple(text),
        character_start_times_seconds=tuple(i * duration / len(text) for i in range(len(text))),
        character_end_times_seconds=tuple((i + 1) * duration / len(text) for i in range(len(text))),
    )


def test_provider_payload_timestamp_parsing_and_default_voice():
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.elevenlabs_speech import ElevenLabsSpeechProvider

    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["xi-api-key"] == "test-key"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "voices": [
                        {"voice_id": "cloned", "name": "George", "category": "cloned"},
                        {
                            "voice_id": "stock",
                            "name": "George - Warm Storyteller",
                            "category": "premade",
                        },
                    ]
                },
            )
        assert request.url.path == "/v1/text-to-speech/stock/with-timestamps"
        assert request.url.params["output_format"] == "mp3_44100_128"
        payload = json.loads(request.content)
        assert payload["text"] == "Hello world."
        assert payload["model_id"] == "eleven_multilingual_v2"
        assert payload["voice_settings"]["speed"] == 1.0
        return httpx.Response(
            200,
            json={
                "audio_base64": base64.b64encode(b"audio").decode(),
                "alignment": alignment().model_dump(),
            },
            headers={"request-id": "speech-123", "character-cost": "12"},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = ElevenLabsSpeechProvider("test-key", client)
        assert provider.select_voice()[0] == "stock"
        result = provider.generate("Hello world.", SpeechSettings(voice_id="stock"))
    assert result.content == b"audio"
    assert result.metadata.character_cost == 12
    assert result.metadata.request_id == "speech-123"
    assert len(requests) == 2


@pytest.mark.parametrize("mode", ["status", "timeout", "invalid_audio", "invalid_alignment"])
def test_provider_errors_are_safe_and_never_retried(mode):
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.elevenlabs_speech import ElevenLabsSpeechProvider

    calls = []

    def handle(request):
        calls.append(request)
        if mode == "status":
            return httpx.Response(401, text="test-secret should never be displayed")
        if mode == "timeout":
            raise httpx.ReadTimeout("test-secret", request=request)
        data = {
            "audio_base64": base64.b64encode(b"audio").decode(),
            "alignment": alignment().model_dump(),
        }
        if mode == "invalid_audio":
            data["audio_base64"] = "not base64!"
        else:
            data["alignment"]["character_start_times_seconds"] = [0]
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MediaError) as exc:
            ElevenLabsSpeechProvider("test-secret", client).generate(
                "Hello", SpeechSettings(voice_id="stock")
            )
    assert "test-secret" not in str(exc.value)
    assert len(calls) == 1


def test_alignment_rejects_nonfinite_or_reversed_timestamps():
    data = alignment().model_dump()
    data["character_start_times_seconds"] = (
        float("nan"),
        *data["character_start_times_seconds"][1:],
    )
    with pytest.raises(ValidationError):
        SpeechAlignment.model_validate(data)
    data = alignment().model_dump()
    data["character_end_times_seconds"] = tuple(reversed(data["character_end_times_seconds"]))
    with pytest.raises(ValidationError):
        SpeechAlignment.model_validate(data)


def test_caption_times_follow_trim_speed_and_scene_offset():
    cue = NarrationCue(cue_id="save_01", text="Hello world.", start=9, duration=1)
    plan = speech_plan(cue, SpeechMetadata(alignment=alignment(duration=0.9)), 1, 1.35)
    assert 1 < plan["tempo"] <= 1.35
    assert plan["captions"][0]["start"] == pytest.approx(9.12)
    assert plan["captions"][-1]["end"] < 10
    assert "00:00:09,120 -->" in srt_text(plan["captions"])
    with pytest.raises(MediaError, match="Shorten the script"):
        speech_plan(cue, SpeechMetadata(alignment=alignment(duration=3)), 3.1, 1.35)


@pytest.fixture(scope="module")
def narration_assets(local_assets):  # noqa: F811 -- pytest injects the imported fixture
    root, sources, collection, _ = local_assets
    output = ReelRenderer().render(
        collection,
        RenderSettings(
            width=216, height=384, intro_seconds=1, countdown_seconds=1, clip_seconds=0.5
        ),
        source_runs_dir=sources,
        runs_dir=root / "base",
        run_id="base",
    )
    tone = root / "speech.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=0.22",
            "-c:a",
            "libmp3lame",
            str(tone),
        ],
        check=True,
        capture_output=True,
    )
    script = NarrationScript(
        intro=INTRO_SCRIPT,
        games=[{"title": g.title, "text": "Enter this world."} for g in collection.games],
    )
    return output.parent, tone.read_bytes(), script


class FakeSpeech:
    def __init__(self, content, fail=False):
        self.content, self.fail, self.calls = content, fail, 0

    def generate(self, text, settings):
        self.calls += 1
        if self.fail:
            raise MediaError("speech timeout")
        return GeneratedSpeech(self.content, SpeechMetadata(alignment=alignment(text, 0.2)))


def test_real_mix_subtitles_and_keyless_cached_resume(narration_assets, tmp_path, monkeypatch):
    render_dir, audio, script = narration_assets
    provider = FakeSpeech(audio)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(
        render_dir, script, SpeechSettings(voice_id="test"), runs_dir=tmp_path, run_id="narrated"
    )
    original = hashlib.sha256((render_dir / "reel.mp4").read_bytes()).hexdigest()
    output = pipeline.execute(store)
    run = pipeline.load(store)
    assert provider.calls == 5
    assert run.status == StageStatus.COMPLETED
    assert all(cue.attempted and cue.stage.status == StageStatus.COMPLETED for cue in run.cues)
    intro_end = run.effective_intro_seconds
    assert float(probe_media(output)["format"]["duration"]) == pytest.approx(
        intro_end + 3, abs=1 / 24
    )
    assert (store.run_dir / "subtitles.srt").read_text().count("-->") == 6
    assert "drawtext" in (store.run_dir / "mix_filter.txt").read_text()

    # Check actual burned captions, not just filter construction.
    def frame(path):
        return subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "0.167",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        ).stdout

    region = slice(155 * 216 * 3, 210 * 216 * 3)
    assert frame(output)[region] != frame(render_dir / "reel.mp4")[region]
    plan = json.loads((store.run_dir / "speech_plan.json").read_text())
    assert [c["text"] for c in plan[0]["captions"]] == [
        "You have died",
        "You must pick a game cartridge to be reincarnated into",
    ]
    assert 0 <= intro_end - plan[0]["captions"][-1]["end"] < 1 / 24
    raw_audio = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    samples = array("f")
    samples.frombytes(raw_audio)

    def peak(start, end):
        return max(abs(v) for v in samples[round(start * 48000) : round(end * 48000)])

    assert peak(0.15, 0.25) > 0.01  # Intro speech
    assert peak(0.5, 0.9) < 0.001
    assert peak(intro_end + 0.01, intro_end + 0.1) > 0.01  # Countdown follows spoken intro
    for start in (intro_end + 1.15 + i * 0.5 for i in range(4)):
        assert peak(start, start + 0.1) > 0.01
    assert hashlib.sha256((render_dir / "reel.mp4").read_bytes()).hexdigest() == original
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert NarrationPipeline(None).execute(store) == output
    if importlib.util.find_spec("dotenv") and importlib.util.find_spec("httpx"):
        assert (
            main(
                ["resume-narration", str(store.run_dir), "--env-file", str(tmp_path / "absent.env")]
            )
            == 0
        )
    assert provider.calls == 5
    (store.run_dir / "speech/intro.mp3").write_bytes(b"changed")
    with pytest.raises(MediaError, match="checksum"):
        NarrationPipeline(None).execute(store)


def test_ambiguous_paid_failure_is_not_repeated(narration_assets, tmp_path):
    render_dir, audio, script = narration_assets
    provider = FakeSpeech(audio, fail=True)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(render_dir, script, SpeechSettings(voice_id="test"), runs_dir=tmp_path)
    with pytest.raises(MediaError, match="speech timeout"):
        pipeline.execute(store)
    assert pipeline.load(store).status == StageStatus.FAILED
    with pytest.raises(MediaError, match="No duplicate"):
        pipeline.execute(store)
    assert provider.calls == 1
    assert not (store.run_dir / ".narration.lock").exists()


def test_local_failure_reuses_all_paid_speech(narration_assets, tmp_path, monkeypatch):
    render_dir, audio, script = narration_assets
    provider = FakeSpeech(audio)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(
        render_dir, script, SpeechSettings(voice_id="test", subtitles=False), runs_dir=tmp_path
    )
    with monkeypatch.context() as patch:

        def fail(*args):
            raise MediaError("local encoder failure")

        patch.setattr(pipeline.renderer, "_ffmpeg", fail)
        with pytest.raises(MediaError, match="local encoder"):
            pipeline.execute(store)
    assert provider.calls == 5
    output = NarrationPipeline(None).execute(store)
    assert output.is_file()
    assert provider.calls == 5
    assert "drawtext" not in (store.run_dir / "mix_filter.txt").read_text()


def test_script_mismatch_is_rejected_before_creating_run(narration_assets, tmp_path):
    render_dir, _, script = narration_assets
    bad_script = script.model_copy(deep=True)
    bad_script.games[0].title = "WRONG GAME"
    with pytest.raises(ValueError, match="titles and order"):
        NarrationPipeline(None).prepare(
            render_dir, bad_script, SpeechSettings(voice_id="test"), runs_dir=tmp_path / "output"
        )
    assert not (tmp_path / "output").exists()


def test_cli_missing_key_creates_no_run(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    pytest.importorskip("dotenv")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert (
        main(
            [
                "narrate",
                "--render-run",
                str(tmp_path / "missing-render"),
                "--script",
                str(tmp_path / "missing-script"),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--env-file",
                str(tmp_path / "absent.env"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "runs").exists()


def test_intro_captions_use_two_complete_sentences_and_respect_pause():
    text = INTRO_SCRIPT.split()
    words = [
        {
            "text": word,
            "start": i * 0.2 + (0.5 if i >= 3 else 0),
            "end": (i + 1) * 0.2 + (0.5 if i >= 3 else 0),
        }
        for i, word in enumerate(text)
    ]
    captions = timed_intro_captions(words)
    assert len(captions) == 2
    assert captions[0]["end"] == pytest.approx(0.6)
    assert captions[1]["start"] == pytest.approx(1.1)
    assert captions[1]["end"] == words[-1]["end"]
    words[-1]["text"] = "something-else"
    with pytest.raises(ValueError, match="must match"):
        timed_intro_captions(words)


def test_reuse_copies_matching_speech_and_generates_only_changed_line(narration_assets, tmp_path):
    render_dir, audio, script = narration_assets
    original = NarrationPipeline(FakeSpeech(audio))
    settings = SpeechSettings(voice_id="test")
    cached = original.prepare(render_dir, script, settings, runs_dir=tmp_path, run_id="cached")
    original.execute(cached)
    updated = script.model_copy(deep=True)
    updated.games[0].text = "A different reveal."
    provider = FakeSpeech(audio)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(
        render_dir,
        updated,
        settings,
        runs_dir=tmp_path,
        run_id="updated",
        reuse_speech_dir=cached.run_dir,
    )
    pending = pipeline.load(store)
    assert [c.audio is not None for c in pending.cues] == [True, False, True, True, True]
    assert pending.reused_speech_run_id == "cached"
    pipeline.execute(store)
    assert provider.calls == 1
    assert (store.run_dir / "speech/save_02.mp3").read_bytes() == (
        cached.run_dir / "speech/save_02.mp3"
    ).read_bytes()


@pytest.mark.parametrize("status,code,rejected", [
    (429, "too_many_concurrent_requests", True),
    (429, "system_busy", True),
    (401, "invalid_api_key", True),
    (422, "unknown-code-with-private-content", True),
    (500, "system_busy", False),
    (408, "unknown", False),
])
def test_speech_rejections_are_typed_without_exposing_response_body(status, code, rejected):
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.elevenlabs_speech import ElevenLabsSpeechProvider
    from save_reel.providers.speech import SpeechRequestRejected

    def handle(request):
        return httpx.Response(
            status, json={"detail": {"status": code, "message": "private-secret"}},
            headers={"Retry-After": "7"},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MediaError) as caught:
            ElevenLabsSpeechProvider("private-secret", client).generate(
                "Hello", SpeechSettings(voice_id="stock")
            )
    error = caught.value
    assert isinstance(error, SpeechRequestRejected) == rejected
    assert "private" not in str(error)
    if rejected:
        assert error.status_code == status
        assert error.retryable == (status == 429)
        assert error.retry_after == 7
        assert error.code is None or error.code == code


@pytest.mark.parametrize("persistent", [False, True])
def test_429_waits_for_batch_then_retries_only_rejected_speech(
    narration_assets, tmp_path, monkeypatch, persistent,  # noqa: F811
):
    from save_reel.providers.speech import SpeechRequestRejected

    source, audio, script = narration_assets
    waits = []

    class BusySpeech(FakeSpeech):
        rejected = 0
        reject = True

        def generate(self, text, settings):
            if text == script.intro and self.reject and (persistent or self.rejected == 0):
                self.rejected += 1
                raise SpeechRequestRejected(
                    "ElevenLabs rejected the request (HTTP 429). Too many voice requests.",
                    status_code=429, code="too_many_concurrent_requests", retry_after=3,
                )
            return super().generate(text, settings)

    provider = BusySpeech(audio)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(source, script, SpeechSettings(voice_id="test"),
                             runs_dir=tmp_path, run_id="limited")

    def wait(delay):
        state = pipeline.load(store)
        # The rejected intro is retryable; every other request has finished and persisted.
        assert not state.cues[0].attempted
        assert state.cues[0].failure.outcome == "rejected"
        assert all(c.audio and c.metadata for c in state.cues[1:])
        waits.append(delay)

    monkeypatch.setattr("save_reel.narration.time.sleep", wait)
    # Other composition behavior is already covered by the real FFmpeg tests.
    monkeypatch.setattr(pipeline, "_compose", lambda *args: None)
    if persistent:
        with pytest.raises(MediaError, match="resume this job"):
            pipeline.execute(store, max_workers=4)
        assert provider.rejected == 3
        state = pipeline.load(store)
        assert state.cues[0].failure.http_status == 429
        assert state.cues[0].failure.retryable
        assert not state.cues[0].attempted
        assert provider.calls == 4
        provider.reject = False
        pipeline.execute(store, max_workers=4)
    else:
        pipeline.execute(store, max_workers=4)
        assert provider.rejected == 1
    assert waits == ([3, 4] if persistent else [3])
    assert provider.calls == 5  # Four saves once, plus the accepted intro once.
    state = pipeline.load(store)
    assert state.status == "completed"
    assert all(c.audio and c.failure is None for c in state.cues)


def test_ambiguous_resume_preserves_original_failure(narration_assets, tmp_path):  # noqa: F811
    source, audio, script = narration_assets
    provider = FakeSpeech(audio, fail=True)
    pipeline = NarrationPipeline(provider)
    store = pipeline.prepare(source, script, SpeechSettings(voice_id="test"), runs_dir=tmp_path)
    with pytest.raises(MediaError, match="speech timeout"):
        pipeline.execute(store)
    original = pipeline.load(store).cues[0].failure
    assert original.outcome == "uncertain"
    assert not original.retryable
    for _ in range(2):
        with pytest.raises(MediaError, match="Original failure: speech timeout"):
            pipeline.execute(store)
    current = pipeline.load(store).cues[0]
    assert current.failure == original
    assert current.stage.error == "speech timeout"
    assert provider.calls == 1
