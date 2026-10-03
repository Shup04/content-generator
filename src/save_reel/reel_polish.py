"""Reusable FFmpeg scenes: cartridge reveal, bounded slowdown and still fallback."""

import math
import textwrap

from save_reel.ffmpeg_text import draw_text


def frame_seconds(seconds, fps):
    return math.ceil(seconds * fps - 1e-8) / fps


def title_scene(settings, number, title, duration):
    p, theme = settings.polish, settings.opener
    style, scale = p.style, settings.width / 1080
    def px(n):
        return max(1, round(n * scale))
    pad = px(theme.overscan)
    bob = settings.bobbing[number - 1]
    inputs = []
    for path in ("ui/starfield.png", "ui/halo.png", f"inputs/save_{number:02}/cartridge.png"):
        inputs += ["-loop", "1", "-framerate", str(settings.fps), "-t", str(duration), "-i", path]
    label = draw_text(f"save_{number:02}", px(style.label_size), "(w-text_w)/2",
                      str(px(style.card_y - 80)), font="ui/system.ttf", color=style.accent)
    name = draw_text(f"title_{number:02}", px(min(72, 1350 / max(1, len(title)))),
                     "(w-text_w)/2", str(px(style.card_title_y)), font="ui/display.ttf",
                     color=theme.text_color)
    motion = f"sin(t*{bob.speed}+{bob.phase})*{bob.amplitude * scale}"
    graph = (
        f"[0:v]crop={settings.width}:{settings.height}:"
        f"x='{pad}+sin(t*{theme.star_drift_speed})*{theme.star_drift * scale}':"
        f"y='{pad}+cos(t*{theme.star_drift_speed})*{theme.star_drift * scale}',"
        "setsar=1,setpts=PTS-STARTPTS[bg];"
        f"[1:v]scale={px(style.card_width)}:{px(style.card_height)},format=rgba[h];"
        f"[2:v]scale={px(style.card_width)}:{px(style.card_height)}:"
        "force_original_aspect_ratio=decrease,format=rgba,setsar=1[c];"
        f"[bg][h]overlay=x=(W-w)/2:y='{px(style.card_y)}+{motion}':shortest=1[b];"
        f"[b][c]overlay=x=(W-w)/2:y='{px(style.card_y)}+{motion}':shortest=1,"
        f"{label},{name},format=yuv420p"
    )
    fade = min(p.transition_seconds, duration / 4)
    if fade:
        graph += f",fade=t=out:st={duration - fade}:d={fade}"
    return inputs, graph + "[outv]"


def plan_footage(durations, target, fps, max_slowdown, allow_still_fallback=True):
    """Budget whole frames. Each source appears at most once; excess uses a still."""
    target_frames = round(target * fps)
    if not durations or min(durations) <= 0 or target_frames < 1:
        raise ValueError("Positive clip durations and target frames are required")
    slowdown = min(max_slowdown, max(1, target / sum(durations)))
    capacities = [max(1, math.floor(d * slowdown * fps + 1e-6)) for d in durations]
    if target_frames < len(durations):
        raise ValueError("Save section must have at least one frame per B-roll beat")
    # Short speech still shows every beat: divide its frame budget proportionally,
    # rather than exhausting the budget on clip A and silently omitting clip B.
    allocations = capacities[:]
    if sum(capacities) > target_frames:
        remaining = target_frames - len(durations)
        allocations = [1 + math.floor(remaining * d / sum(durations)) for d in durations]
        for index in range(target_frames - sum(allocations)):
            allocations[index % len(allocations)] += 1
    remaining, result = target_frames, []
    for index, duration in enumerate(durations):
        frames = min(remaining, allocations[index])
        if frames:
            result.append(dict(kind="video", index=index, frames=frames, slowdown=slowdown))
            remaining -= frames
    if remaining:
        if not allow_still_fallback:
            raise ValueError("Narration exceeds available footage at the maximum slowdown. "
                             "Enable still fallback or shorten the narration; clips will not loop.")
        result.append(dict(kind="still", index=len(durations) - 1, frames=remaining, slowdown=1))
    return result


def lower_third(settings, number, title):
    p, theme = settings.polish, settings.opener
    style, scale = p.style, settings.width / 1080
    def px(n):
        return max(1, round(n * scale))
    x, y, width, height = px(style.margin), px(style.lower_y), settings.width - 2 * px(
        style.margin
    ), px(style.lower_height)
    enable = f"lt(t,{p.lower_third_seconds})"
    return [
        f"drawbox=x={x}:y={y}:w={width}:h={height}:color={style.panel}@"
        f"{style.panel_opacity}:t=fill:enable='{enable}'",
        f"drawbox=x={x}:y={y}:w={px(3)}:h={height}:color={style.accent}:t=fill:"
        f"enable='{enable}'",
        draw_text(f"save_{number:02}", px(style.label_size), str(x + px(24)), str(y + px(16)),
                  font="ui/system.ttf", color=style.accent, enable=enable),
        draw_text(f"title_{number:02}", px(min(style.title_size, 1300 / max(1, len(title)))),
                  str(x + px(24)), str(y + px(59)), font="ui/display.ttf",
                  color=theme.text_color, enable=enable),
    ]


def world_scene(renderer, store, settings, number, title, duration, clip_count):
    from save_reel.rendering import probe_media

    paths = [f"inputs/save_{number:02}/broll_{i:02}.mp4" for i in range(1, clip_count + 1)]
    durations = [float(probe_media(store.run_dir / p, renderer.ffprobe)["format"]["duration"])
                 for p in paths]
    plan = plan_footage(durations, duration, settings.fps, settings.polish.max_slowdown,
                        settings.polish.allow_still_fallback)
    if settings.polish.still_position == "before" and plan[-1]["kind"] == "still":
        plan = [plan[-1], *plan[:-1]]
    inputs, graphs, labels = [], [], []
    for i, item in enumerate(plan):
        frames = item["frames"]
        source = paths[item["index"]]
        if item["kind"] == "still":
            still = source.replace(".mp4", "_still.png")
            if not (store.run_dir / still).exists() and (
                store.run_dir / source.replace(".mp4", ".png")
            ).exists():
                still = source.replace(".mp4", ".png")  # Older render input layout.
            if not (store.run_dir / still).exists():
                # Older runs may lack a still. Extract a frame locally, never call an API.
                renderer._ffmpeg(store, f"fallback_{number:02}", [
                    "-sseof", "-0.1", "-i", source, "-frames:v", "1", "-update", "1", still,
                ])
            inputs += ["-i", still]
            zoom = settings.polish.still_zoom
            # Upscale before zoompan to reduce integer-pixel judder during subtle movement.
            chain = (
                f"scale={settings.width * 2}:{settings.height * 2}:"
                "force_original_aspect_ratio=increase,"
                f"crop={settings.width * 2}:{settings.height * 2},"
                f"zoompan=z='1+{zoom}*on/{max(1, frames - 1)}':"
                "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                f"d={frames}:s={settings.width}x{settings.height}:fps={settings.fps}"
            )
        else:
            inputs += ["-i", source]
            chain = (
                f"setpts={item['slowdown']}*(PTS-STARTPTS),"
                f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,"
                f"crop={settings.width}:{settings.height},fps={settings.fps},"
                # fps rounding can lose the final frame; this pads at most one frame.
                f"tpad=stop_mode=clone:stop_duration={1 / settings.fps}"
            )
        graphs.append(f"[{i}:v]{chain},trim=end_frame={frames},setsar=1,"
                      f"settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
        labels.append(f"[v{i}]")
        item["source"] = source if item["kind"] == "video" else still
    graphs.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[world]")
    filters = [f"fps={settings.fps}",
               f"tpad=stop_mode=clone:stop_duration={2 / settings.fps}",
               f"trim=end_frame={round(duration * settings.fps)}"]
    if settings.polish.transition_seconds:
        filters.append(f"fade=t=in:st=0:d={settings.polish.transition_seconds}")
    if settings.show_clip_titles:
        filters += lower_third(settings, number, title)
    graphs.append("[world]" + ",".join(filters) + "[outv]")
    return inputs, ";\n".join(graphs), plan


def caption_filter(store, run, settings, caption, index):
    style, scale = settings.polish.style, settings.width / 1080
    def px(n):
        return max(1, round(n * scale))
    name = f"subtitle_{index:03}"
    text = textwrap.fill(caption["text"], width=style.caption_wrap)
    run.artifacts[name] = store.write_text(f"text/{name}.txt", text)
    x, y = px(style.margin), px(style.caption_y)
    width = settings.width - 2 * x
    height = px((style.caption_size + 10) * len(text.splitlines()) + 2 * style.caption_padding)
    enable = f"gte(t,{caption['start']})*lt(t,{caption['end']})"
    return [
        f"drawbox=x={x}:y={y}:w={width}:h={height}:color={style.panel}@"
        f"{style.panel_opacity}:t=fill:enable='{enable}'",
        f"drawbox=x={x}:y={y}:w={width}:h={px(2)}:color={style.accent}@"
        f"{style.border_opacity}:t=fill:enable='{enable}'",
        draw_text(name, px(style.caption_size), "(w-text_w)/2", str(y + px(style.caption_padding)),
                  font="ui/system.ttf", color=settings.opener.text_color,
                  line_spacing=px(10), enable=enable),
    ]
