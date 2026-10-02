"""Deterministic starfield assets and a narration-ready retro-console opener."""

import math
import random
import struct
import textwrap
import zlib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from save_reel.ffmpeg_text import draw_text
from save_reel.opener_style import INTRO_CAPTIONS, OpenerStyle
from save_reel.storage import RunStore

if TYPE_CHECKING:
    from save_reel.render_models import RenderSettings


def png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\0" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def rgb(color: str) -> tuple[int, int, int]:
    return tuple(bytes.fromhex(color.removeprefix("#")))


def starfield(settings: "RenderSettings") -> bytes:
    style, scale = settings.opener, settings.width / 1080
    pad = max(2, round(style.overscan * scale))
    width, height = settings.width + 2 * pad, settings.height + 2 * pad
    background = rgb(style.background)
    pixels = bytearray(bytes((*background, 255)) * (width * height))
    rng = random.Random(style.star_seed)
    for _ in range(style.star_count):
        x, y = rng.randrange(width), rng.randrange(height)
        radius = max(0.6, rng.uniform(style.star_radius_min, style.star_radius_max) * scale)
        opacity = rng.uniform(style.star_opacity_min, style.star_opacity_max)
        for dy in range(-math.ceil(radius), math.ceil(radius) + 1):
            for dx in range(-math.ceil(radius), math.ceil(radius) + 1):
                if 0 <= x + dx < width and 0 <= y + dy < height:
                    alpha = opacity * max(0, 1 - math.hypot(dx, dy) / (radius + 0.4))
                    colour = bytes(
                        round(b + (s - b) * alpha)
                        for b, s in zip(background, rgb(style.star_color), strict=True)
                    )
                    position = ((y + dy) * width + x + dx) * 4
                    pixels[position : position + 3] = colour
    return png(width, height, pixels)


def halo(style: OpenerStyle) -> bytes:
    # Small generated alpha texture; FFmpeg scales it to each card's design-space halo.
    width, height = 180, 120
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            radius = ((x - width / 2) / (width / 2)) ** 2 + ((y - height / 2) / (height / 2)) ** 2
            opacity = style.separation_opacity * max(0, 1 - radius) ** 2
            pixels.extend((*rgb(style.separation_color), round(255 * opacity)))
    return png(width, height, pixels)


def prepare_opener(store: RunStore, settings: "RenderSettings") -> dict:
    style = settings.opener
    artifacts = {}
    for role, custom, bundled in (
        ("display", style.display_font_file, "Oxanium.ttf"),
        ("system", style.system_font_file, "IBMPlexMono-Regular.ttf"),
    ):
        content = (
            Path(custom).read_bytes()
            if custom
            else files("save_reel").joinpath("assets", "fonts", bundled).read_bytes()
        )
        artifacts[f"ui_{role}_font"] = store.write_bytes(f"ui/{role}.ttf", content, "font/ttf")
    artifacts["ui_starfield"] = store.write_bytes(
        "ui/starfield.png", starfield(settings), "image/png"
    )
    artifacts["ui_halo"] = store.write_bytes("ui/halo.png", halo(style), "image/png")
    return artifacts


def opener_inputs(settings: "RenderSettings") -> list[str]:
    duration = settings.intro_seconds + settings.countdown_seconds
    paths = [f"inputs/save_{i:02}/cartridge.png" for i in range(1, 5)]
    paths.extend(["ui/starfield.png", "ui/halo.png"])
    args = []
    for path in paths:
        args.extend(
            ["-loop", "1", "-framerate", str(settings.fps), "-t", str(duration), "-i", path]
        )
    return args


def opener_filter(settings: "RenderSettings", captions: list[dict] | None = None) -> str:
    style, scale = settings.opener, settings.width / 1080

    def px(n):
        return max(1, round(n * scale))

    pad = max(2, round(style.overscan * scale))
    enabled = f"gte(t,{settings.intro_seconds})"
    graph = [
        f"[4:v]format=rgba,crop={settings.width}:{settings.height}:"
        f"x='{pad}+sin(t*{style.star_drift_speed})*{style.star_drift * scale}':"
        f"y='{pad}+cos(t*{style.star_drift_speed})*{style.star_drift * scale}',"
        "setsar=1,setpts=PTS-STARTPTS[space]",
        f"[5:v]scale={px(style.card_width + 2 * style.halo_padding)}:"
        f"{px(style.card_height + 2 * style.halo_padding)},format=rgba,split=4[h0][h1][h2][h3]",
    ]
    previous = "space"
    for i, bob in enumerate(settings.bobbing):
        x, y = px(style.column_x[i % 2]), px(style.row_y[i // 2])
        motion = (
            f"sin((t-{settings.intro_seconds})*{bob.speed}+{bob.phase})*{bob.amplitude * scale}"
        )
        graph.extend(
            [
                f"[{previous}][h{i}]overlay=x={x - px(style.halo_padding)}:"
                f"y='{y - px(style.halo_padding)}+{motion}':eval=frame:"
                f"alpha=straight:format=auto:shortest=1:enable='{enabled}'[halo{i}]",
                f"[{i}:v]scale={px(style.card_width)}:{px(style.card_height)}:flags=lanczos,"
                f"format=rgba,setsar=1,setpts=PTS-STARTPTS[card{i}]",
                f"[halo{i}][card{i}]overlay=x={x}:y='{y}+{motion}':eval=frame:"
                f"alpha=straight:format=auto:shortest=1:enable='{enabled}'[grid{i}]",
            ]
        )
        previous = f"grid{i}"
    text = []
    for i in range(4):
        x, y = style.column_x[i % 2], style.row_y[i // 2]
        text.append(
            draw_text(
                f"save_{i + 1:02}",
                px(style.label_size),
                f"{px(x + style.card_width / 2)}-text_w/2",
                str(px(y + style.card_height + style.label_gap)),
                font="ui/system.ttf",
                color=f"{style.system_color}@{style.system_opacity}",
                enable=enabled,
            )
        )
        # Four restrained corner marks define a console slot without enclosing it in a panel.
        for right in (False, True):
            for bottom in (False, True):
                cx = x + (style.card_width - style.bracket_inset if right else style.bracket_inset)
                cy = y + (style.card_height if bottom else 0)
                color = f"{style.system_color}@{style.bracket_opacity}"
                text.extend(
                    [
                        f"drawbox=x={px(cx) - (px(style.bracket_length) if right else 0)}:"
                        f"y={px(cy)}:"
                        f"w={px(style.bracket_length)}:h={px(style.bracket_stroke)}:"
                        f"color={color}:t=fill:enable='{enabled}'",
                        f"drawbox=x={px(cx)}:"
                        f"y={px(cy) - (px(style.bracket_length) if bottom else 0)}:"
                        f"w={px(style.bracket_stroke)}:h={px(style.bracket_length)}:"
                        f"color={color}:t=fill:enable='{enabled}'",
                    ]
                )
    text.append(
        draw_text(
            "countdown_caption",
            px(style.choice_label_size),
            "(w-text_w)/2",
            str(px(style.choice_label_y)),
            font="ui/system.ttf",
            color=f"{style.system_color}@{style.system_opacity}",
            enable=enabled,
        )
    )
    for index in range(settings.countdown_seconds):
        start = settings.intro_seconds + index
        text.append(
            draw_text(
                f"count_{settings.countdown_seconds - index}",
                px(style.countdown_size),
                "(w-text_w)/2",
                str(px(style.countdown_y)),
                font="ui/display.ttf",
                color=style.text_color,
                enable=f"gte(t,{start})*lt(t,{start + 1})",
            )
        )
    for i, caption in enumerate(captions or []):
        text.append(
            draw_text(
                f"opener_caption_{i}",
                px(style.intro_first_size if i == 0 else style.intro_second_size),
                "(w-text_w)/2",
                f"{px(style.intro_center_y)}-text_h/2",
                font="ui/display.ttf",
                color=style.text_color,
                line_spacing=px(style.intro_line_spacing),
                enable=f"gte(t,{caption['start']})*lt(t,{caption['end']})",
            )
            + f":text_align={style.intro_alignment}"
        )
    graph.append(f"[{previous}]" + ",".join(text) + ",format=yuv420p,setsar=1[outv]")
    return ";\n".join(graph)


def write_intro_captions(store: RunStore, style: OpenerStyle, captions: list[dict]) -> dict:
    return {
        f"opener_caption_{i}": store.write_text(
            f"text/opener_caption_{i}.txt", textwrap.fill(caption["text"], width=style.intro_wrap)
        )
        for i, caption in enumerate(captions)
    }


def timed_intro_captions(words: list[dict]) -> list[dict]:
    expected = [word for phrase in INTRO_CAPTIONS for word in phrase.lower().split()]
    actual = [reword(w["text"]) for w in words]
    if actual != expected:
        raise ValueError("Console intro narration must match the two configured intro sentences")
    split = len(INTRO_CAPTIONS[0].split())
    return [
        {
            "text": INTRO_CAPTIONS[0],
            "start": words[0]["start"],
            "end": words[split - 1]["end"],
            "cue_id": "intro",
        },
        {
            "text": INTRO_CAPTIONS[1],
            "start": words[split]["start"],
            "end": words[-1]["end"],
            "cue_id": "intro",
        },
    ]


def reword(word: str) -> str:
    return word.strip(".,!?;:").lower()
