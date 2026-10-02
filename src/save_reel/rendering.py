"""Local FFmpeg composition of cartridge selection, countdown, and B-roll reveals."""

import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from array import array
from fractions import Fraction
from pathlib import Path

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.ffmpeg_text import draw_text
from save_reel.models import Artifact, StageState, StageStatus, utc_now
from save_reel.opener import opener_filter, opener_inputs, prepare_opener
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.pipeline import run_logging
from save_reel.providers.media import MediaError
from save_reel.render_models import RenderCollection, RenderRun, RenderSettings, build_timeline
from save_reel.storage import RunStore


def probe_media(path: Path, ffprobe: str = "ffprobe") -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise MediaError(f"Cannot inspect media file: {path}")
    return json.loads(result.stdout)


def resolve_font(font_file: Path | None) -> Path:
    if font_file is not None:
        if not font_file.is_file():
            raise MediaError(f"Font file does not exist: {font_file}")
        return font_file.resolve()
    for candidate in (
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if Path(candidate).is_file():
            return Path(candidate)
    raise MediaError("No suitable font found. Pass --font-file /path/to/font.ttf")


def write_countdown_audio(path: Path, settings: RenderSettings, duration: float) -> None:
    """One stereo UI tone per digit; silence leaves room for future narration/music."""
    sample_rate = 48000
    samples = array("h", [0]) * (round(duration * sample_rate) * 2)
    for index in range(settings.countdown_seconds):
        start = round((settings.intro_seconds + index) * sample_rate)
        frequency = 880 if index == settings.countdown_seconds - 1 else 660
        for offset in range(round(0.16 * sample_rate)):
            t = offset / sample_rate
            envelope = min(1, t / 0.004) * math.exp(-35 * t)
            tone = math.sin(2 * math.pi * frequency * t)
            value = round(32767 * settings.countdown_volume * envelope * tone)
            position = (start + offset) * 2
            samples[position] = samples[position + 1] = value
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


def intro_filter(settings: RenderSettings) -> str:
    return opener_filter(settings)


def clip_filter(settings: RenderSettings, number: int, title: str) -> str:
    s = settings.width / 1080
    filters = [
        f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase:"
        "force_divisible_by=2",
        f"crop={settings.width}:{settings.height}",
        "setsar=1",
        "setpts=PTS-STARTPTS",
        f"fps={settings.fps}",
        f"trim=duration={settings.clip_seconds}",
    ]
    if settings.show_clip_titles:
        enable = f"lt(t,{min(1.5, settings.clip_seconds)})"
        filters.extend(
            [
                f"drawbox=x={round(48 * s)}:y={round(1518 * s)}:w={round(984 * s)}:"
                f"h={round(170 * s)}:color=black@0.48:t=fill:enable='{enable}'",
                draw_text(
                    f"save_{number:02}",
                    round(30 * s),
                    str(round(76 * s)),
                    str(round(1540 * s)),
                    color="0xbbece3",
                    enable=enable,
                ),
                draw_text(
                    f"title_{number:02}",
                    max(1, round(min(60, 1400 / len(title)) * s)),
                    str(round(76 * s)),
                    str(round(1594 * s)),
                    enable=enable,
                ),
            ]
        )
    return "[0:v]" + ",".join(filters) + ",format=yuv420p[outv]\n"


class ReelRenderer:
    def __init__(self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    @staticmethod
    def _save(store: RunStore, run: RenderRun) -> None:
        store.write_text(
            "render.json",
            RenderRun.model_validate(run).model_dump_json(indent=2) + "\n",
            "application/json",
        )

    @staticmethod
    def _record(store: RunStore, relative: str) -> Artifact:
        path = store.run_dir / relative
        media_type = {
            ".mp4": "video/mp4",
            ".png": "image/png",
            ".wav": "audio/wav",
        }.get(path.suffix, "text/plain")
        return Artifact(
            path=relative,
            media_type=media_type,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def _source(self, store: RunStore, artifact: Artifact) -> Path:
        path = store.run_dir / artifact.path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
            raise MediaError(f"Source asset is missing or its checksum changed: {path}")
        return path

    def render(
        self,
        collection: RenderCollection,
        settings: RenderSettings,
        *,
        source_runs_dir: Path = Path("runs"),
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
        font_file: Path | None = None,
    ) -> Path:
        versions = {}
        for name, executable in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)):
            resolved = shutil.which(executable)
            if resolved is None:
                raise MediaError(f"{name} is not installed or not on PATH")
            resolved = str(Path(resolved).resolve())
            setattr(self, name, resolved)
            try:
                result = subprocess.run(
                    [resolved, "-version"], capture_output=True, text=True, check=True, timeout=30
                )
            except (subprocess.SubprocessError, OSError):
                raise MediaError(f"Cannot run {name} -version") from None
            versions[name] = result.stdout.splitlines()[0]
        font = resolve_font(font_file)
        sources = []
        for number, game in enumerate(collection.games, 1):
            source_store = RunStore(source_runs_dir / game.cartridge_run_id)
            cartridge = CartridgePipeline.load(source_store)
            if (
                cartridge.status != StageStatus.COMPLETED
                or cartridge.values.title != game.title
                or cartridge.settings.image_background != "transparent"
            ):
                raise MediaError(
                    f"{game.title}: a completed matching transparent cartridge is required"
                )
            sources.append(
                (
                    self._source(source_store, cartridge.artifacts["image"]),
                    f"inputs/save_{number:02}/cartridge.png",
                )
            )
            for clip_number, broll_id in enumerate(game.broll_run_ids, 1):
                broll_store = RunStore(source_runs_dir / broll_id)
                broll = BrollPipeline.load(broll_store)
                if broll.status != StageStatus.COMPLETED or broll.values.game_title != game.title:
                    raise MediaError(f"{game.title}: a completed matching B-roll run is required")
                path = self._source(broll_store, broll.artifacts["video"])
                info = probe_media(path, self.ffprobe)
                videos = [v for v in info.get("streams", []) if v.get("codec_type") == "video"]
                if not videos or float(videos[0].get("duration", info["format"]["duration"])) < (
                    settings.clip_seconds - 0.001
                ):
                    raise MediaError(
                        f"B-roll is shorter than {settings.clip_seconds} seconds: {path}"
                    )
                sources.append((path, f"inputs/save_{number:02}/broll_{clip_number:02}.mp4"))
        store = RunStore.create(runs_dir, run_id)
        run = RenderRun(
            run_id=store.run_dir.name,
            collection=collection,
            settings=settings,
            timeline=build_timeline(collection, settings),
            narration_text=INTRO_SCRIPT,
            tool_versions=versions,
        )
        self._save(store, run)
        with run_logging(store) as logger:
            active = "prepare"
            try:
                run.status = StageStatus.RUNNING
                run.stages[active] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                self._save(store, run)
                for source, relative in sources:
                    run.artifacts[relative] = store.write_bytes(
                        relative,
                        source.read_bytes(),
                        "image/png" if relative.endswith(".png") else "video/mp4",
                    )
                run.artifacts["font"] = store.write_bytes("font.ttf", font.read_bytes(), "font/ttf")
                run.artifacts.update(prepare_opener(store, settings))
                texts = {"countdown_caption": "MAKE YOUR CHOICE"}
                for number, game in enumerate(collection.games, 1):
                    texts[f"save_{number:02}"] = f"SAVE {number:02}"
                    texts[f"title_{number:02}"] = game.title
                texts.update(
                    {f"count_{i}": str(i) for i in range(1, settings.countdown_seconds + 1)}
                )
                for name, text in texts.items():
                    run.artifacts[f"text/{name}"] = store.write_text(f"text/{name}.txt", text)
                total = run.timeline[-1].start + run.timeline[-1].duration
                write_countdown_audio(store.run_dir / "audio/countdown.wav", settings, total)
                run.artifacts["countdown_audio"] = self._record(store, "audio/countdown.wav")
                run.stages[active].status = StageStatus.COMPLETED
                run.stages[active].finished_at = utc_now()
                self._save(store, run)

                def encode_stage(name: str, inputs: list[str], graph: str, duration: float) -> str:
                    nonlocal active
                    active = name
                    run.stages[name] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                    run.artifacts[f"filter/{name}"] = store.write_text(f"filters/{name}.txt", graph)
                    self._save(store, run)
                    logger.info("Rendering %s (%.2fs)", name, duration)
                    relative = f"segments/{name}.mp4"
                    (store.run_dir / "segments").mkdir(exist_ok=True)
                    self._ffmpeg(
                        store,
                        name,
                        [
                            *inputs,
                            "-filter_complex",
                            graph,
                            "-map",
                            "[outv]",
                            "-an",
                            "-frames:v",
                            str(round(duration * settings.fps)),
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
                            "-video_track_timescale",
                            "24000",
                            relative,
                        ],
                    )
                    run.artifacts[name] = self._record(store, relative)
                    run.stages[name].status = StageStatus.COMPLETED
                    run.stages[name].finished_at = utc_now()
                    self._save(store, run)
                    return relative

                intro_duration = settings.intro_seconds + settings.countdown_seconds
                intro_inputs = opener_inputs(settings)
                segments = [
                    encode_stage("intro", intro_inputs, intro_filter(settings), intro_duration)
                ]
                for segment in run.timeline[2:]:
                    number, clip_number = segment.save_number, segment.clip_number
                    name = f"save_{number:02}_clip_{clip_number:02}"
                    segments.append(
                        encode_stage(
                            name,
                            [
                                "-i",
                                f"inputs/save_{number:02}/broll_{clip_number:02}.mp4",
                            ],
                            clip_filter(settings, number, segment.title),
                            segment.duration,
                        )
                    )
                active = "assemble"
                run.stages[active] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                run.artifacts["concat"] = store.write_text(
                    "segments.txt", "".join(f"file '{path}'\n" for path in segments)
                )
                self._save(store, run)
                logger.info("Assembling %.2fs reel with countdown audio", total)
                self._ffmpeg(
                    store,
                    active,
                    [
                        "-f",
                        "concat",
                        "-safe",
                        "1",
                        "-i",
                        "segments.txt",
                        "-i",
                        "audio/countdown.wav",
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c:v",
                        "copy",
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
                run.stages[active].status = StageStatus.COMPLETED
                run.stages[active].finished_at = utc_now()
                active = "validate"
                run.stages[active] = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                self._save(store, run)
                info = probe_media(store.run_dir / "reel.partial.mp4", self.ffprobe)
                video = next(v for v in info["streams"] if v["codec_type"] == "video")
                audio = next(v for v in info["streams"] if v["codec_type"] == "audio")
                if (
                    video["width"] != settings.width
                    or video["height"] != settings.height
                    or abs(float(info["format"]["duration"]) - total) > 1 / settings.fps
                    or int(video["nb_frames"]) != round(total * settings.fps)
                    or Fraction(video["avg_frame_rate"]) != settings.fps
                    or video["codec_name"] != "h264"
                    or video["pix_fmt"] != "yuv420p"
                    or audio["codec_name"] != "aac"
                ):
                    raise MediaError(
                        "Rendered reel failed output format, frame count, or timing checks"
                    )
                (store.run_dir / "reel.partial.mp4").replace(store.run_dir / "reel.mp4")
                run.artifacts["reel"] = self._record(store, "reel.mp4")
                run.artifacts["probe"] = store.write_text(
                    "probe.json", json.dumps(info, indent=2) + "\n", "application/json"
                )
                run.stages[active].status = StageStatus.COMPLETED
                run.stages[active].finished_at = utc_now()
                run.status = StageStatus.COMPLETED
                self._save(store, run)
                logger.info("Reel completed: %s", store.run_dir / "reel.mp4")
                return store.run_dir / "reel.mp4"
            except Exception as exc:
                message = str(exc) if isinstance(exc, (MediaError, OSError)) else type(exc).__name__
                run.status = StageStatus.FAILED
                run.stages[active].status = StageStatus.FAILED
                run.stages[active].error = message
                run.stages[active].finished_at = utc_now()
                self._save(store, run)
                raise MediaError(message) from None

    def _ffmpeg(self, store: RunStore, name: str, args: list[str]) -> None:
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-n",
            "-filter_complex_threads",
            "1",
            "-filter_threads",
            "1",
            *args,
        ]
        store.write_text(
            f"command_{name}.json", json.dumps(command, indent=2) + "\n", "application/json"
        )
        log_path = store.run_dir / f"ffmpeg_{name}.log"
        with log_path.open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=store.run_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    timeout=1800,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise MediaError(f"FFmpeg timed out during {name}. See {log_path}") from None
        if result.returncode:
            raise MediaError(f"FFmpeg failed during {name}. See {log_path}")
