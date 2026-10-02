"""Add a manually authored voice track and timed captions to an existing reel."""

import hashlib
import json
import math
import re
import shutil
import textwrap
from contextlib import contextmanager
from pathlib import Path

from save_reel.models import Artifact, StageState, StageStatus, utc_now
from save_reel.narration_models import (
    NarrationCue,
    NarrationRun,
    NarrationScript,
    SpeechMetadata,
    SpeechSettings,
    narration_cues,
)
from save_reel.opener import (
    opener_filter,
    opener_inputs,
    timed_intro_captions,
    write_intro_captions,
)
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.pipeline import run_logging
from save_reel.providers.media import MediaError
from save_reel.providers.speech import SpeechProvider
from save_reel.render_models import RenderRun, build_timeline
from save_reel.rendering import ReelRenderer, draw_text, probe_media, write_countdown_audio
from save_reel.storage import RunStore


def checked_bytes(store: RunStore, artifact: Artifact) -> bytes:
    content = (store.run_dir / artifact.path).read_bytes()
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise MediaError(f"Saved artifact checksum changed: {artifact.path}")
    return content


def speech_plan(
    cue: NarrationCue, metadata: SpeechMetadata, duration: float, max_tempo: float
) -> dict:
    alignment = metadata.alignment
    spoken = [i for i, character in enumerate(alignment.characters) if character.strip()]
    first = alignment.character_start_times_seconds[spoken[0]]
    last = alignment.character_end_times_seconds[spoken[-1]]
    if last > duration + 0.1:
        raise MediaError(f"{cue.cue_id}: speech timestamps exceed the saved audio duration")
    trim_start, trim_end = max(0, first - 0.04), min(duration, last + 0.08)
    available = cue.duration - 0.24
    tempo = max(1.0, (trim_end - trim_start) / available)
    if tempo > max_tempo:
        raise MediaError(
            f"{cue.cue_id}: narration needs {tempo:.2f}x speed to fit; limit is {max_tempo:.2f}x. "
            "Shorten the script or use a longer render. Saved speech has been retained."
        )
    # Flatten character chunks so provider normalization can expand numbers into words.
    chars = []
    starts = []
    ends = []
    for character, start, end in zip(
        alignment.characters,
        alignment.character_start_times_seconds,
        alignment.character_end_times_seconds,
        strict=True,
    ):
        chars.extend(character)
        starts.extend([start] * len(character))
        ends.extend([end] * len(character))
    offset = cue.start + 0.12
    words = [
        {
            "text": match.group(),
            "start": offset + (starts[match.start()] - trim_start) / tempo,
            "end": offset + (ends[match.end() - 1] - trim_start) / tempo,
        }
        for match in re.finditer(r"\S+", "".join(chars))
    ]
    captions, group = [], []
    for word in words:
        if group and (len(group) >= 5 or len(" ".join(w["text"] for w in [*group, word])) > 38):
            captions.append(
                {
                    "text": " ".join(w["text"] for w in group),
                    "start": group[0]["start"],
                    "end": group[-1]["end"],
                }
            )
            group = []
        group.append(word)
        if word["text"].endswith((".", "?", "!")):
            captions.append(
                {
                    "text": " ".join(w["text"] for w in group),
                    "start": group[0]["start"],
                    "end": group[-1]["end"],
                }
            )
            group = []
    if group:
        captions.append(
            {
                "text": " ".join(w["text"] for w in group),
                "start": group[0]["start"],
                "end": group[-1]["end"],
            }
        )
    for caption in captions:
        caption["cue_id"] = cue.cue_id
    return {
        "cue_id": cue.cue_id,
        "trim_start": trim_start,
        "trim_end": trim_end,
        "tempo": tempo,
        "offset": offset,
        "captions": captions,
        "words": words,
    }


def srt_text(captions: list[dict]) -> str:
    def timestamp(seconds: float) -> str:
        ms = round(seconds * 1000)
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"

    return "\n".join(
        f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}\n"
        for i, c in enumerate(captions, 1)
    )


class NarrationPipeline:
    def __init__(
        self, provider: SpeechProvider | None, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"
    ) -> None:
        self.provider = provider
        self.renderer = ReelRenderer(ffmpeg=ffmpeg, ffprobe=ffprobe)

    @staticmethod
    def load(store: RunStore) -> NarrationRun:
        run = NarrationRun.model_validate_json(
            (store.run_dir / "narration.json").read_text(encoding="utf-8")
        )
        if run.run_id != store.run_dir.name:
            raise MediaError("Narration run ID does not match its directory")
        return run

    @staticmethod
    def _save(store: RunStore, run: NarrationRun) -> None:
        store.write_text(
            "narration.json",
            NarrationRun.model_validate(run).model_dump_json(indent=2) + "\n",
            "application/json",
        )

    def _check_tools(self) -> None:
        for name in ("ffmpeg", "ffprobe"):
            executable = shutil.which(getattr(self.renderer, name))
            if executable is None:
                raise MediaError(f"{name} is required for narration assembly")
            setattr(self.renderer, name, str(Path(executable).resolve()))

    def prepare(
        self,
        render_dir: Path,
        script: NarrationScript,
        settings: SpeechSettings,
        *,
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
        reuse_speech_dir: Path | None = None,
    ) -> RunStore:
        self._check_tools()
        source = RunStore(render_dir)
        render = RenderRun.model_validate_json((render_dir / "render.json").read_text())
        if render.status != StageStatus.COMPLETED or render.run_id != render_dir.name:
            raise MediaError("A completed matching render run is required")
        cues = narration_cues(render, script)
        console = "ui_display_font" in render.artifacts
        if console and script.intro != INTRO_SCRIPT:
            raise MediaError(f"Console intro script must be: {INTRO_SCRIPT}")
        video = checked_bytes(source, render.artifacts["reel"])
        font = checked_bytes(source, render.artifacts["font"])
        store = RunStore.create(runs_dir, run_id)
        run = NarrationRun(
            run_id=store.run_dir.name,
            source_render=render,
            script=script,
            settings=settings,
            cues=cues,
        )
        run.artifacts["source_video"] = store.write_bytes("source.mp4", video, "video/mp4")
        run.artifacts["font"] = store.write_bytes("font.ttf", font, "font/ttf")
        run.artifacts["script"] = store.write_text(
            "script.json", script.model_dump_json(indent=2), "application/json"
        )
        if console:
            for key, artifact in render.artifacts.items():
                if (
                    key.startswith("ui_")
                    or artifact.path.endswith("/cartridge.png")
                    or key.startswith(("text/save_", "text/count"))
                ):
                    run.artifacts[key] = store.write_bytes(
                        artifact.path, checked_bytes(source, artifact), artifact.media_type
                    )
        if reuse_speech_dir:
            cached_store = RunStore(reuse_speech_dir)
            cached = self.load(cached_store)
            generation_fields = {"voice_id", "model_id", "speed", "stability", "similarity_boost"}
            if cached.settings.model_dump(include=generation_fields) != settings.model_dump(
                include=generation_fields
            ):
                raise MediaError("Cached speech voice/model/settings do not match this narration")
            run.reused_speech_run_id = cached.run_id
            for cue in run.cues:
                match = next(
                    (
                        c
                        for c in cached.cues
                        if c.cue_id == cue.cue_id
                        and c.text == cue.text
                        and c.stage.status == StageStatus.COMPLETED
                    ),
                    None,
                )
                if match and match.audio and match.metadata:
                    cue.audio = store.write_bytes(
                        f"speech/{cue.cue_id}.mp3",
                        checked_bytes(cached_store, match.audio),
                        "audio/mpeg",
                    )
                    cue.metadata = store.write_bytes(
                        f"speech/{cue.cue_id}.json",
                        checked_bytes(cached_store, match.metadata),
                        "application/json",
                    )
                    cue.attempted = True
                    cue.stage = match.stage.model_copy(deep=True)
        self._save(store, run)
        return store

    @staticmethod
    @contextmanager
    def _lock(store: RunStore):
        path = store.run_dir / ".narration.lock"
        try:
            path.mkdir()
        except FileExistsError:
            raise MediaError(
                "Narration run is locked; confirm any previous process has stopped"
            ) from None
        try:
            yield
        finally:
            path.rmdir()

    def execute(self, store: RunStore) -> Path:
        self._check_tools()
        with self._lock(store), run_logging(store) as logger:
            run = self.load(store)
            for artifact in run.artifacts.values():
                checked_bytes(store, artifact)
            for cue in run.cues:
                for artifact in (cue.audio, cue.metadata):
                    if artifact:
                        checked_bytes(store, artifact)
            if run.status == StageStatus.COMPLETED:
                return store.run_dir / run.artifacts["reel"].path
            active = "speech"
            current = None
            try:
                run.status = StageStatus.RUNNING
                run.stages[active] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                self._save(store, run)
                for cue in run.cues:
                    current = cue
                    if cue.audio and cue.metadata:
                        logger.info("Reusing narration: %s", cue.cue_id)
                        continue
                    if cue.attempted:
                        raise MediaError(
                            f"{cue.cue_id}: speech request was attempted without a saved result; "
                            "check ElevenLabs history before starting a new run. No duplicate sent."
                        )
                    if self.provider is None:
                        raise MediaError("An ElevenLabs speech provider is required")
                    cue.attempted = True
                    cue.stage = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                    self._save(store, run)
                    logger.info(
                        "Generating narration: %s (%d characters)", cue.cue_id, len(cue.text)
                    )
                    speech = self.provider.generate(cue.text, run.settings)
                    cue.audio = store.write_bytes(
                        f"speech/{cue.cue_id}.mp3", speech.content, "audio/mpeg"
                    )
                    cue.metadata = store.write_text(
                        f"speech/{cue.cue_id}.json",
                        speech.metadata.model_dump_json(indent=2),
                        "application/json",
                    )
                    cue.stage.status = StageStatus.COMPLETED
                    cue.stage.finished_at = utc_now()
                    self._save(store, run)
                current = None
                run.stages[active].status = StageStatus.COMPLETED
                run.stages[active].finished_at = utc_now()
                active = "compose"
                run.stages[active] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                self._save(store, run)
                self._compose(store, run, logger)
                run.stages[active].status = StageStatus.COMPLETED
                run.stages[active].finished_at = utc_now()
                run.status = StageStatus.COMPLETED
                self._save(store, run)
                logger.info("Narrated reel completed: %s", store.run_dir / "reel.mp4")
                return store.run_dir / "reel.mp4"
            except Exception as exc:
                message = str(exc) if isinstance(exc, (MediaError, OSError)) else type(exc).__name__
                run.status = StageStatus.FAILED
                stage = run.stages[active]
                stage.status, stage.error, stage.finished_at = (
                    StageStatus.FAILED,
                    message,
                    utc_now(),
                )
                if current and current.stage.status != StageStatus.COMPLETED:
                    current.stage.status = StageStatus.FAILED
                    current.stage.error = message
                    current.stage.finished_at = utc_now()
                self._save(store, run)
                raise MediaError(message) from None

    def _compose(self, store: RunStore, run: NarrationRun, logger) -> None:
        plans, inputs = [], []
        console = "ui_display_font" in run.artifacts
        settings = run.source_render.settings.model_copy(deep=True)
        for cue in run.cues:
            metadata = SpeechMetadata.model_validate_json(checked_bytes(store, cue.metadata))
            info = probe_media(store.run_dir / cue.audio.path, self.renderer.ffprobe)
            if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
                raise MediaError(f"{cue.cue_id}: generated file has no audio stream")
            audio_duration = float(info["format"]["duration"])
            if console and cue.cue_id == "intro":
                # Let the voice set the opening length instead of compressing it to a fixed slot.
                natural = cue.model_copy(update={"duration": audio_duration + 1})
                plan = speech_plan(natural, metadata, audio_duration, run.settings.max_tempo)
                plan["captions"] = timed_intro_captions(plan["words"])
                settings.intro_seconds = (
                    math.ceil(plan["captions"][-1]["end"] * settings.fps) / settings.fps
                )
                run.effective_intro_seconds = settings.intro_seconds
                run.timeline = build_timeline(run.source_render.collection, settings)
                shifted = narration_cues(
                    run.source_render.model_copy(
                        update={"settings": settings, "timeline": run.timeline}
                    ),
                    run.script,
                )
                for saved, new in zip(run.cues, shifted, strict=True):
                    saved.start, saved.duration = new.start, new.duration
            else:
                plan = speech_plan(cue, metadata, audio_duration, run.settings.max_tempo)
            plans.append(plan)
            inputs.extend(["-i", cue.audio.path])
            logger.info(
                "Fitting %s at %.2fx, starting %.2fs", cue.cue_id, plan["tempo"], plan["offset"]
            )
        timeline = run.timeline or run.source_render.timeline
        total = timeline[-1].start + timeline[-1].duration
        captions = [caption for plan in plans for caption in plan["captions"]]
        run.artifacts["plan"] = store.write_text(
            "speech_plan.json", json.dumps(plans, indent=2), "application/json"
        )
        run.artifacts["subtitles"] = store.write_text(
            "subtitles.srt", srt_text(captions), "application/x-subrip"
        )
        graph = []
        for i, plan in enumerate(plans):
            graph.append(
                f"[{i}:a]atrim=start={plan['trim_start']}:end={plan['trim_end']},"
                f"asetpts=PTS-STARTPTS,atempo={plan['tempo']},aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={round(plan['offset'] * 1000)}:all=1[voice{i}]"
            )
        graph.append(
            "".join(f"[voice{i}]" for i in range(len(plans)))
            + f"amix=inputs={len(plans)}:normalize=0,asetpts=N/SR/TB,apad=whole_dur={total},"
            + f"atrim=end_sample={round(total * 48000)},"
            "loudnorm=I=-16:TP=-2:LRA=7,aresample=48000[outa]"
        )
        voice_graph = ";\n".join(graph)
        run.artifacts["voice_filter"] = store.write_text("voice_filter.txt", voice_graph)
        # Only derived outputs are replaced on local retries; paid speech stays intact.
        for name in ("narration.wav", "reel.partial.mp4"):
            (store.run_dir / name).unlink(missing_ok=True)
        self.renderer._ffmpeg(
            store,
            "narration_track",
            [
                *inputs,
                "-filter_complex",
                voice_graph,
                "-map",
                "[outa]",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-t",
                str(total),
                "narration.wav",
            ],
        )
        run.artifacts["narration"] = store.write_bytes(
            "narration.wav", (store.run_dir / "narration.wav").read_bytes(), "audio/wav"
        )
        self._save(store, run)
        extra_inputs = []
        video_graph = ""
        video_source = "0:v"
        base_audio = "0:a"
        if console:
            intro_captions = plans[0]["captions"]
            run.artifacts.update(write_intro_captions(store, settings.opener, intro_captions))
            graph = opener_filter(settings, intro_captions)
            run.artifacts["opener_filter"] = store.write_text("opener_filter.txt", graph)
            (store.run_dir / "opener.mp4").unlink(missing_ok=True)
            self.renderer._ffmpeg(
                store,
                "synced_opener",
                [
                    *opener_inputs(settings),
                    "-filter_complex",
                    graph,
                    "-map",
                    "[outv]",
                    "-an",
                    "-frames:v",
                    str(
                        round((settings.intro_seconds + settings.countdown_seconds) * settings.fps)
                    ),
                    "-r",
                    str(settings.fps),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-threads",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    "opener.mp4",
                ],
            )
            run.artifacts["opener"] = store.write_bytes(
                "opener.mp4", (store.run_dir / "opener.mp4").read_bytes(), "video/mp4"
            )
            write_countdown_audio(store.run_dir / "countdown.wav", settings, total)
            run.artifacts["countdown"] = store.write_bytes(
                "countdown.wav", (store.run_dir / "countdown.wav").read_bytes(), "audio/wav"
            )
            extra_inputs = ["-i", "opener.mp4", "-i", "countdown.wav"]
            base_audio = "3:a"
            old_start = (
                run.source_render.settings.intro_seconds
                + run.source_render.settings.countdown_seconds
            )
            video_graph = (
                f"[0:v]trim=start={old_start},setpts=PTS-STARTPTS[reveals];"
                "[2:v]setpts=PTS-STARTPTS[opening];"
                f"[opening][reveals]concat=n=2:v=1:a=0,fps={settings.fps}[composed];"
            )
            video_source = "composed"
        mix_graph = (
            f"[{base_audio}]aresample=48000[base];[base][1:a]amix=inputs=2:duration=first:normalize=0,"
            "alimiter=limit=0.95:level=false:latency=true[outa]"
        )
        video_args = ["-map", "0:v:0", "-c:v", "copy"]
        if run.settings.subtitles or console:
            scale = run.source_render.settings.width / 1080
            filters = []
            for i, caption in enumerate(captions):
                if console and (caption["cue_id"] == "intro" or not run.settings.subtitles):
                    continue
                name = f"subtitle_{i:03}"
                content = textwrap.fill(caption["text"], width=32)
                run.artifacts[name] = store.write_text(f"text/{name}.txt", content)
                y = 505 if caption["cue_id"] == "intro" else 1340
                filters.append(
                    draw_text(
                        name,
                        round(46 * scale),
                        "(w-text_w)/2",
                        str(round(y * scale)),
                        enable=f"gte(t,{caption['start']})*lt(t,{caption['end']})",
                    )
                    + f":box=1:boxcolor=black@0.65:boxborderw={max(2, round(12 * scale))}"
                )
            mix_graph += (
                ";" + video_graph + f"[{video_source}]" + (",".join(filters) or "null") + "[outv]"
            )
            video_args = [
                "-map",
                "[outv]",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
            ]
        run.artifacts["mix_filter"] = store.write_text("mix_filter.txt", mix_graph)
        self.renderer._ffmpeg(
            store,
            "narrated_reel",
            [
                "-i",
                "source.mp4",
                "-i",
                "narration.wav",
                *extra_inputs,
                "-filter_complex",
                mix_graph,
                *video_args,
                "-map",
                "[outa]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-t",
                str(total),
                "-movflags",
                "+faststart",
                "reel.partial.mp4",
            ],
        )
        info = probe_media(store.run_dir / "reel.partial.mp4", self.renderer.ffprobe)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        if (
            abs(float(info["format"]["duration"]) - total) > 1 / settings.fps
            or int(video["nb_frames"]) != round(total * settings.fps)
            or (video["width"], video["height"]) != (settings.width, settings.height)
            or audio["codec_name"] != "aac"
        ):
            raise MediaError("Narrated output failed duration, dimensions, or audio checks")
        (store.run_dir / "reel.partial.mp4").replace(store.run_dir / "reel.mp4")
        run.artifacts["reel"] = store.write_bytes(
            "reel.mp4", (store.run_dir / "reel.mp4").read_bytes(), "video/mp4"
        )
        run.artifacts["probe"] = store.write_text(
            "probe.json", json.dumps(info, indent=2), "application/json"
        )
