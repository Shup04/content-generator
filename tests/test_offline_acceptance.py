"""Offline Studio acceptance: actual FFmpeg, synthetic inputs, network forbidden."""

import hashlib
import json
import re
import socket
import subprocess
from array import array

import pytest
from test_narration import alignment
from test_rendering import frame, pixel, png_cutout
from test_studio import prepare_job
from test_studio_scripts import FakeWriter

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.media_models import BrollSettings
from save_reel.narration import NarrationPipeline
from save_reel.narration_models import SpeechMetadata
from save_reel.polish_models import ReelPolish
from save_reel.providers.media import GeneratedImage, MediaError, VideoTask
from save_reel.providers.speech import GeneratedSpeech
from save_reel.render_models import RenderSettings
from save_reel.rendering import probe_media
from save_reel.storage import RunStore
from save_reel.studio_models import StudioDraft
from save_reel.studio_store import StudioStore, is_finished_reel
from save_reel.studio_worker import ProductionJob

COLOURS = ((230, 35, 45), (30, 220, 65), (40, 55, 230), (230, 195, 35),
           (200, 40, 200), (35, 200, 200), (220, 110, 30), (110, 50, 220))


def ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', *map(str, args)],
                          check=True, capture_output=True).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Offline acceptance attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    for name in ('OPENAI_API_KEY', 'MINIMAX_API_KEY', 'ELEVENLABS_API_KEY'):
        monkeypatch.delenv(name, raising=False)


def test_studio_four_world_two_beat_acceptance(tmp_path, monkeypatch):
    """Checkpointed task interruption, fresh worker resume, and final MP4 checks."""
    store = StudioStore(tmp_path / 'project')  # Never reads a checkout .env or runs.
    clips = {}
    for index, rgb in enumerate(COLOURS):
        path = tmp_path / f'clip-{index}.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0x{bytes(rgb).hex()}:s=216x384:r=24:d=0.5',
               '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', path)
        clips[index] = path.read_bytes()
    tone = tmp_path / 'tone.mp3'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=3.2',
           '-c:a', 'libmp3lame', tone)
    calls = dict(cartridges=0, stills=0, submissions=0, speech=0)
    writer = FakeWriter()
    monkeypatch.setattr('save_reel.story_cli._provider', lambda *args: writer)

    class Image:
        def generate(self, prompt, settings):
            key = 'cartridges' if settings.image_background == 'transparent' else 'stills'
            calls[key] += 1
            # Deliberately synthetic cutouts: acceptance is not an art-quality benchmark.
            return GeneratedImage(png_cutout(COLOURS[(calls[key] - 1) % 8]))

    class Video:
        interrupted = False

        def submit(self, image, prompt, settings):
            assert calls['stills'] == 8
            calls['submissions'] += 1
            return f'offline-task-{calls["submissions"]}'

        def query(self, task_id):
            if task_id == 'offline-task-3' and not self.interrupted:
                self.interrupted = True
                raise MediaError('Injected interruption after task ID checkpoint')
            return VideoTask('succeeded', task_id)

        def download(self, task_id):
            return clips[int(task_id.rsplit('-', 1)[1]) - 1]

    video = Video()

    def command(self, verb, target, *args):
        if verb == 'resume-cartridge':
            CartridgePipeline(Image()).execute(RunStore(target))
        else:
            assert verb == 'resume-broll'
            BrollPipeline(Image(), video).execute(
                RunStore(target), image_only='--image-only' in args)

    monkeypatch.setattr(ProductionJob, 'command', command)
    draft = store.create('Offline acceptance: fictional worlds and synthetic media')
    draft.seed = 41
    draft.parallel_saves = draft.speech_parallel_saves = 1
    draft.broll = BrollSettings(visual_mode='videos', clips_per_save=2)
    draft.render = RenderSettings(width=216, height=384, intro_seconds=1,
                                  countdown_seconds=1, clip_seconds=3,
                                  polish=ReelPolish(enabled=True, still_position='after'))
    draft.speech.voice_id = 'offline-tone'
    draft = store.save(draft)

    class Speech:
        def generate(self, text, settings):
            calls['speech'] += 1
            titles = {g.title for g in store.load(draft.draft_id).games}
            duration = (1.2 if text == draft.render.opener.intro_script else
                        0.6 if text.rstrip('.') in titles else 3)
            return GeneratedSpeech(tone.read_bytes(), SpeechMetadata(
                alignment=alignment(text, duration)))

    # The fake key exercises the existing orchestration boundary. No provider is constructed.
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-placeholder')
    monkeypatch.setattr('save_reel.providers.elevenlabs_speech.ElevenLabsSpeechProvider',
                        lambda *args: Speech())
    job = prepare_job(store, draft, 'full', job_id='offline-acceptance')
    job.execute()
    assert job.job['status'] == 'failed'
    assert 'Injected interruption' in job.job['error']
    assert calls == dict(cartridges=4, stills=8, submissions=3, speech=0)
    assert not any(row['finished'] for row in store.list_runs())
    completed = {p: digest(p) for p in store.runs.rglob('*')
                 if p.suffix in ('.png', '.mp4')}
    assert sum(p.suffix == '.mp4' for p in completed) == 2
    pending = [BrollPipeline.load(RunStore(p.parent)) for p in store.runs.glob('*/broll.json')]
    assert any(r.video_task_id == 'offline-task-3' and 'video' not in r.artifacts for r in pending)
    text_calls = len(writer.calls)

    # A new worker reads checkpoints from disk, like Studio's Resume action.
    resumed = ProductionJob(store, job.job_id)
    resumed.execute()
    assert resumed.job['status'] == 'completed', resumed.job.get('error')
    assert calls == dict(cartridges=4, stills=8, submissions=8, speech=9)
    assert len(writer.calls) == text_calls
    assert all(digest(path) == old for path, old in completed.items())
    assert resumed.job['progress']['completed'] == resumed.job['progress']['total']
    output = store.runs / resumed.job['outputs']['reel']
    result = output / 'reel.mp4'
    run = NarrationPipeline.load(RunStore(output))
    duration = max(scene.start + scene.duration for scene in run.timeline)
    assert is_finished_reel(output)
    assert [row['run_id'] for row in store.list_runs() if row['finished']] == [output.name]
    info = probe_media(result)
    streams = info['streams']
    assert {s['codec_type'] for s in streams} == {'audio', 'video'}
    visual = next(s for s in streams if s['codec_type'] == 'video')
    assert (visual['width'], visual['height'], visual['codec_name']) == (216, 384, 'h264')
    assert float(info['format']['duration']) == pytest.approx(duration, abs=0.1)
    ffmpeg('-i', result, '-f', 'null', '-')  # Decode the full video and audio streams.

    plans = json.loads((output / 'speech_plan.json').read_text())
    assert {p['cue_id'] for p in plans} == {'intro', *(f'title_{i:02}' for i in range(1, 5)),
                                         *(f'save_{i:02}' for i in range(1, 5))}
    samples = array('h', ffmpeg('-i', result, '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'))
    for plan in plans:
        midpoint = plan['offset'] + (plan['trim_end'] - plan['trim_start']) / plan['tempo'] / 2
        window = samples[round((midpoint - 0.1) * 8000):round((midpoint + 0.1) * 8000)]
        assert window and (sum(s * s for s in window) / len(window)) ** 0.5 / 32768 > 0.01
        assert all(0 <= w['start'] < w['end'] <= duration for w in plan['words'])
    footage = json.loads((output / 'footage_plan.json').read_text())
    for number, parts in enumerate(footage.values(), 1):
        assert [p['kind'] for p in parts] == ['video', 'video', 'still']
        assert [p['index'] for p in parts] == [0, 1, 1]
        assert all(p['slowdown'] <= 1.5 for p in parts)
        scene = next(s for s in run.timeline if s.kind == 'broll' and s.save_number == number)
        title = next(s for s in run.timeline if s.kind == 'save_intro' and s.save_number == number)
        speech_title = next(p for p in plans if p['cue_id'] == f'title_{number:02}')
        assert title.start <= speech_title['words'][0]['start']
        assert speech_title['words'][-1]['end'] <= scene.start
        offset = scene.start
        for beat, part in enumerate(parts[:2]):
            image = frame(result, offset + part['frames'] / 24 / 2)
            assert pixel(image, 108, 192) == pytest.approx(COLOURS[(number - 1) * 2 + beat], abs=10)
            offset += part['frames'] / 24
    assert all('-stream_loop' not in p.read_text() for p in output.glob('command_*.json'))
    subtitles = (output / 'subtitles.srt').read_text()
    times = re.findall(r'(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})', subtitles)
    assert times
    expected_captions = sorted(
        (caption for plan in plans for caption in plan['captions']), key=lambda c: c['start'])
    assert len(times) == len(expected_captions)
    previous = 0
    for values, expected in zip(times, expected_captions, strict=True):
        start, end = [int(values[i]) * 3600 + int(values[i+1]) * 60 + int(values[i+2])
                      + int(values[i+3]) / 1000 for i in (0, 4)]
        assert previous <= start < end <= duration
        assert start == pytest.approx(expected['start'], abs=0.001)
        assert end == pytest.approx(expected['end'], abs=0.001)
        previous = end
    # SRT content and burned captions come from the same saved timestamped plan.
    for plan in plans:
        for caption in plan.get('captions', []):
            assert caption['text'] in subtitles
    assert 'drawtext' in (output / 'mix_filter.txt').read_text()
    original = digest(result)
    ProductionJob(store, job.job_id).execute()
    assert calls == dict(cartridges=4, stills=8, submissions=8, speech=9)
    assert digest(result) == original

    # Eligibility fails closed for interrupted status and missing voice assets.
    manifest = output / 'narration.json'
    raw = manifest.read_bytes()
    data = json.loads(raw)
    data['status'] = 'failed'
    manifest.write_text(json.dumps(data))
    assert not is_finished_reel(output)
    manifest.write_bytes(raw)
    voice = output / 'speech/save_04.mp3'
    audio = voice.read_bytes()
    voice.unlink()
    assert not is_finished_reel(output)
    voice.write_bytes(audio)
    assert is_finished_reel(output)

    # Existing files may be truncated or changed after a successful render.
    # Exercise discovery again after warming its checksum cache each time.
    for artifact in (voice, output / 'speech/save_04.json', result):
        original_bytes = artifact.read_bytes()
        for damaged in (b'', original_bytes[:1], bytes([original_bytes[0] ^ 1])
                        + original_bytes[1:]):
            try:
                assert is_finished_reel(output)
                artifact.write_bytes(damaged)
                assert not is_finished_reel(output), artifact.name
                assert not any(row['finished'] for row in store.list_runs())
            finally:
                artifact.write_bytes(original_bytes)
            assert is_finished_reel(output)
    external_voice = tmp_path / 'external-synthetic-voice.mp3'
    external_voice.write_bytes(audio)
    voice.unlink()
    try:
        voice.symlink_to(external_voice)
        assert not is_finished_reel(output)
    finally:
        voice.unlink()
        voice.write_bytes(audio)
    assert is_finished_reel(output)

    # Load and save a historical single-beat draft without requesting additional visuals.
    legacy = store.load(draft.draft_id).model_dump(mode='json')
    legacy['draft_id'] = 'legacy-one-beat'
    legacy['broll'].pop('stills_per_save')
    legacy['broll'].pop('visual_mode')
    legacy['broll']['clips_per_save'] = 2  # Historical setting ignored absent authored beats.
    for game in legacy['games']:
        game.pop('broll_beats')
    old = StudioDraft.model_validate(legacy)
    assert old.broll.shots_per_save == 1
    assert all(list(g.clip_numbers(old.broll.shots_per_save)) == [1] for g in old.games)
    assert StudioDraft.model_validate_json(old.model_dump_json()) == old
    report = dict(calls=calls, interrupted_task='offline-task-3', source_seed=41,
                  dimensions=[216, 384], duration=float(info['format']['duration']),
                  mp4_sha256=original, speech_cues=9, ordered_clips=8, fallback_worlds=4,
                  legacy_single_beat_preserved=True, finished_library_eligible=True,
                  providers='offline fakes; network connections forbidden')
    (tmp_path / 'acceptance-report.json').write_text(json.dumps(report, indent=2) + '\n')
