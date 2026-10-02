"""Exercise the real FFmpeg boundary with tiny local media and no paid providers."""

import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from array import array
from pathlib import Path

import pytest
from pydantic import ValidationError

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.cli import main
from save_reel.media_models import (
    BrollSettings,
    BrollValues,
    CartridgeValues,
    ImageSettings,
    MotionValues,
)
from save_reel.models import StageStatus
from save_reel.providers.media import MediaError
from save_reel.render_models import RenderCollection, RenderRun, RenderSettings, build_timeline
from save_reel.rendering import ReelRenderer, probe_media, resolve_font


@pytest.fixture
def collection():
    path = Path(__file__).resolve().parents[1] / "examples/cartridges/v2/collection.json"
    return RenderCollection.model_validate_json(path.read_text())


def test_timeline_reserves_narration_and_orders_multiple_clips(collection):
    data = collection.model_dump()
    data["games"][1]["broll_run_ids"] = ("sunroom-clip-1", "sunroom-clip-2")
    collection = RenderCollection.model_validate(data)
    timeline = build_timeline(collection, RenderSettings(intro_seconds=3))
    assert [s.start for s in timeline] == [0, 3, 8, 13, 18, 23, 28]
    assert [s.save_number for s in timeline[2:]] == [1, 2, 2, 3, 4]
    assert [s.clip_number for s in timeline[2:]] == [1, 1, 2, 1, 1]
    assert timeline[4].source_run_id == "sunroom-clip-2"
    assert timeline[-1].start + timeline[-1].duration == 33


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 1081},
        {"height": 1080},
        {"intro_seconds": 0},
        {"countdown_seconds": 0},
        {"clip_seconds": 0.01},
        {"countdown_volume": 1.1},
        {"clip_seconds": float("inf")},
    ],
)
def test_invalid_geometry_or_timing_is_rejected(changes):
    with pytest.raises(ValidationError):
        RenderSettings(**changes)


def test_collection_requires_four_distinct_cartridges(collection):
    data = collection.model_dump()
    data["games"] = data["games"][:3]
    with pytest.raises(ValidationError):
        RenderCollection.model_validate(data)
    data["games"] = (*data["games"], data["games"][0])
    with pytest.raises(ValidationError, match="distinct"):
        RenderCollection.model_validate(data)


def png_cutout(rgb):
    """A solid cartridge rectangle with genuinely transparent surrounding pixels."""

    def chunk(kind, content):
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content))
        )

    width, height = 96, 64
    data = b"".join(
        b"\0"
        + b"".join(
            bytes((*rgb, 255)) if 20 <= x < 76 and 12 <= y < 52 else bytes((255, 0, 255, 0))
            for x in range(width)
        )
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(data))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="module")
def local_assets(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for local integration checks")
    try:
        resolve_font(None)
    except MediaError:
        pytest.skip("No local font available")
    root = tmp_path_factory.mktemp("render-assets")
    sources = root / "sources"
    colours = [(235, 30, 35), (30, 225, 40), (35, 45, 235), (225, 205, 30)]
    games = []
    for index, rgb in enumerate(colours, 1):
        # Include filter metacharacters to exercise textfile/expansion=none handling.
        title = f"GAME {index}: 100% [SAVE]"
        cart_store = CartridgePipeline(None).prepare(
            CartridgeValues(
                title=title,
                shell_material_color="tinted",
                shape_language="chunky",
                molded_details="grooves",
                object_mood="quiet",
                hint_scene_description="one mysterious shape",
            ),
            ImageSettings(image_background="transparent"),
            runs_dir=sources,
            run_id=f"cartridge-{index}",
        )
        cartridge = CartridgePipeline.load(cart_store)
        cartridge.artifacts["image"] = cart_store.write_bytes(
            "cartridge.png", png_cutout(rgb), "image/png"
        )
        cartridge.status = StageStatus.COMPLETED
        CartridgePipeline._save(cart_store, cartridge)
        broll_store = BrollPipeline(None, None).prepare(
            BrollValues(
                game_title=title,
                world_scene="empty world",
                colour_palette="colourful",
                mood="quiet",
                shot_composition="wide view",
                key_surfaces="tiles",
            ),
            MotionValues(camera_motion="slow walk", environment_motion="still", audio="silent"),
            BrollSettings(),
            runs_dir=sources,
            run_id=f"broll-{index}",
        )
        video_path = root / f"source-{index}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x{bytes(rgb).hex()}:s=216x384:r=24:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=200:duration=1",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(video_path),
            ],
            check=True,
            capture_output=True,
        )
        broll = BrollPipeline.load(broll_store)
        broll.artifacts["video"] = broll_store.write_bytes(
            "broll_video.mp4", video_path.read_bytes(), "video/mp4"
        )
        broll.status = StageStatus.COMPLETED
        BrollPipeline._save(broll_store, broll)
        games.append(
            {
                "title": title,
                "values_file": f"{index}.json",
                "cartridge_run_id": f"cartridge-{index}",
                "broll_run_ids": [f"broll-{index}"],
            }
        )
    collection = RenderCollection(
        description="Integration fixture",
        template_version="v2",
        image_background="transparent",
        games=games,
    )
    return root, sources, collection, colours


def frame(path, seconds):
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(seconds),
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
    )
    assert len(result.stdout) == 216 * 384 * 3
    return result.stdout


def pixel(data, x, y):
    offset = (y * 216 + x) * 3
    return tuple(data[offset : offset + 3])


def test_real_render_cli_alpha_motion_order_and_sound(local_assets, monkeypatch):
    root, sources, collection, colours = local_assets
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.chdir(root)
    collection_file = root / "collection.json"
    collection_file.write_text(collection.model_dump_json())
    settings = RenderSettings(
        width=216, height=384, intro_seconds=1, countdown_seconds=2, clip_seconds=0.5
    )
    settings.bobbing[0].amplitude = 30
    settings_file = root / "settings.json"
    settings_file.write_text(settings.model_dump_json())
    args = [
        "render",
        "--collection",
        str(collection_file),
        "--settings",
        str(settings_file),
        "--source-runs-dir",
        str(sources),
        "--runs-dir",
        str(root / "renders"),
        "--run-id",
        "first",
    ]
    assert main(args) == 0
    run_dir = root / "renders/first"
    output = run_dir / "reel.mp4"
    run = RenderRun.model_validate_json((run_dir / "render.json").read_text())
    assert run.status == StageStatus.COMPLETED
    assert all(stage.status == StageStatus.COMPLETED for stage in run.stages.values())
    assert run.artifacts["reel"].sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    info = probe_media(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["nb_frames"]) == (216, 384, "120")
    assert float(info["format"]["duration"]) == pytest.approx(5, abs=1 / 24)
    assert video["avg_frame_rate"] == "24/1"

    opening = frame(output, 0)
    assert all(pixel(opening, x, y)[0] < 30 for x, y in ((55, 160), (161, 245)))
    first = frame(output, 1)
    later = frame(output, 1.75)
    for x, y, rgb in zip((55, 161, 55, 161), (160, 160, 245, 245), colours, strict=True):
        assert pixel(first, x, y) == pytest.approx(rgb, abs=8)
    # Transparent magenta RGB must not appear outside the four visible rectangles.
    assert pixel(first, 10, 135) == pytest.approx((4, 5, 8), abs=8)

    def red_top(data):
        return next(
            y for y in range(125, 185) if pixel(data, 55, y)[0] > 180 and pixel(data, 55, y)[1] < 70
        )

    assert 3 <= red_top(later) - red_top(first) <= 6
    for index, rgb in enumerate(colours):
        assert pixel(frame(output, 3.25 + index * 0.5), 108, 192) == pytest.approx(rgb, abs=8)
    # Countdown digits differ, while keeping the same grid on screen.
    digits = [frame(output, t)[60 * 216 * 3 : 95 * 216 * 3] for t in (1.25, 2.25)]
    assert digits[0] != digits[1]

    audio = subprocess.run(
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
    )
    samples = array("f")
    samples.frombytes(audio.stdout)

    def peak(start, end):
        return max(abs(x) for x in samples[round(start * 48000) : round(end * 48000)])

    assert peak(0, 0.9) < 0.001
    assert peak(1.01, 1.1) > 0.05
    assert peak(1.3, 1.9) < 0.001
    assert peak(2.01, 2.1) > 0.05
    assert peak(3.1, 4.9) < 0.001  # Source clips' 200 Hz audio is removed.
    assert main(args) == 1  # Existing output is never overwritten.
    assert run.artifacts["reel"].sha256 == hashlib.sha256(output.read_bytes()).hexdigest()


def test_checksum_failure_does_not_create_render_run(local_assets, tmp_path):
    _, sources, collection, _ = local_assets
    copied = tmp_path / "sources"
    shutil.copytree(sources, copied)
    (copied / "cartridge-1/cartridge.png").write_bytes(b"changed")
    with pytest.raises(MediaError, match="checksum"):
        ReelRenderer().render(
            collection,
            RenderSettings(clip_seconds=0.5),
            source_runs_dir=copied,
            runs_dir=tmp_path / "renders",
        )
    assert not (tmp_path / "renders").exists()


def test_short_clips_require_opt_in_and_loop_to_full_reveal(local_assets, tmp_path):
    _, sources, collection, colours = local_assets
    renderer = ReelRenderer()
    settings = RenderSettings(
        width=216, height=384, intro_seconds=1, countdown_seconds=1, clip_seconds=2.5
    )
    original = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sources.glob("*/broll_video.mp4")
    }
    with pytest.raises(MediaError, match="shorter"):
        renderer.render(
            collection, settings, source_runs_dir=sources, runs_dir=tmp_path, run_id="rejected"
        )
    assert not (tmp_path / "rejected").exists()

    settings.loop_short_clips = True
    output = renderer.render(
        collection, settings, source_runs_dir=sources, runs_dir=tmp_path, run_id="looped"
    )
    info = probe_media(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert float(info["format"]["duration"]) == pytest.approx(12, abs=1 / 24)
    assert int(video["nb_frames"]) == 288
    for index, expected in enumerate(colours):
        # Check footage well beyond the one-second source duration, in every reveal.
        actual = pixel(frame(output, 2 + index * 2.5 + 2.25), 100, 190)
        assert all(abs(a - b) < 12 for a, b in zip(actual, expected, strict=True))
    assert original == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original}


def test_encode_failure_is_recorded(local_assets, tmp_path, monkeypatch):
    _, sources, collection, _ = local_assets

    def fail(*args):
        raise MediaError("encoder failed")

    monkeypatch.setattr(ReelRenderer, "_ffmpeg", fail)
    with pytest.raises(MediaError, match="encoder failed"):
        ReelRenderer().render(
            collection,
            RenderSettings(clip_seconds=0.5),
            source_runs_dir=sources,
            runs_dir=tmp_path,
            run_id="failed",
        )
    run = json.loads((tmp_path / "failed/render.json").read_text())
    assert run["status"] == "failed"
    assert run["stages"]["intro"]["error"] == "encoder failed"
    assert not (tmp_path / "failed/reel.mp4").exists()
