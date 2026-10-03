"""Speech-timed reveal assembly using the existing renderer and cached speech cues."""

import json

from save_reel.narration_models import SpeechMetadata
from save_reel.opener import (
    opener_filter,
    opener_inputs,
    timed_intro_captions,
    write_intro_captions,
)
from save_reel.providers.media import MediaError
from save_reel.reel_polish import caption_filter, frame_seconds, title_scene, world_scene
from save_reel.render_models import TimelineSegment
from save_reel.rendering import probe_media, write_countdown_audio


def timed_plans(store, run, ffprobe):
    from save_reel.narration import checked_bytes, speech_plan

    settings = run.source_render.settings.model_copy(deep=True)
    plans, timeline = [], []
    cursor = 0
    for cue in run.cues:
        metadata = SpeechMetadata.model_validate_json(checked_bytes(store, cue.metadata))
        checked_bytes(store, cue.audio)
        info = probe_media(store.run_dir / cue.audio.path, ffprobe)
        audio_duration = float(info["format"]["duration"])
        natural = speech_plan(cue.model_copy(update={"start": cursor,
                                                    "duration": audio_duration + 1}),
                              metadata, audio_duration, run.settings.max_tempo)
        span = natural["trim_end"] - natural["trim_start"]
        if cue.cue_id == "intro":
            natural["captions"] = timed_intro_captions(natural["words"])
            duration = frame_seconds(natural["captions"][-1]["end"], settings.fps)
            settings.intro_seconds = run.effective_intro_seconds = duration
            timeline += [TimelineSegment(kind="intro", start=0, duration=duration),
                         TimelineSegment(kind="countdown", start=duration,
                                         duration=settings.countdown_seconds)]
            cursor = duration + settings.countdown_seconds
            cue.start, cue.duration = 0, duration
            plans.append(natural)
            continue
        number = int(cue.cue_id[-2:])
        game = run.source_render.collection.games[number - 1]
        is_title = cue.cue_id.startswith("title_")
        duration = span + 0.24
        if is_title:
            # Aim for 0.8–1.5s. Exceptionally long titles may extend rather than get cut off.
            duration = max(settings.polish.title_seconds, min(1.5, duration),
                           span / run.settings.max_tempo + 0.24)
        duration = frame_seconds(duration, settings.fps)
        cue.start, cue.duration = cursor, duration
        plans.append(speech_plan(cue, metadata, audio_duration, run.settings.max_tempo))
        timeline.append(TimelineSegment(
            kind="save_intro" if is_title else "broll", start=cursor, duration=duration,
            save_number=number, title=game.title,
            source_run_id=None if is_title else game.broll_run_ids[0],
            source_run_ids=() if is_title else game.broll_run_ids,
            clip_number=None if is_title else 1,
        ))
        cursor += duration
    run.timeline = tuple(timeline)
    return settings, plans


def compose_polished(pipeline, store, run, logger):
    from save_reel.narration import srt_text

    renderer = pipeline.renderer
    settings, plans = timed_plans(store, run, renderer.ffprobe)
    total = run.timeline[-1].start + run.timeline[-1].duration
    captions = [c for plan in plans for c in plan["captions"]]
    run.artifacts["plan"] = store.write_text(
        "speech_plan.json", json.dumps(plans, indent=2), "application/json"
    )
    run.artifacts["subtitles"] = store.write_text("subtitles.srt", srt_text(captions),
                                               "application/x-subrip")
    run.artifacts.update(write_intro_captions(store, settings.opener, plans[0]["captions"]))
    pipeline._save(store, run)
    codec = ["-r", str(settings.fps), "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-threads", "2", "-pix_fmt", "yuv420p", "-video_track_timescale", "24000"]

    def encode(name, inputs, graph, duration):
        path = f"segments/{name}.mp4"
        (store.run_dir / "segments").mkdir(exist_ok=True)
        (store.run_dir / path).unlink(missing_ok=True)
        run.artifacts[f"filter/{name}"] = store.write_text(f"filters/{name}.txt", graph)
        logger.info("Rendering %s (%.2fs)", name, duration)
        renderer._ffmpeg(store, name, [*inputs, "-filter_complex", graph, "-map", "[outv]",
                                     "-an", "-frames:v", str(round(duration * settings.fps)),
                                     *codec, path])
        run.artifacts[name] = renderer._record(store, path)
        return path

    paths = [encode("opener", opener_inputs(settings),
                    opener_filter(settings, plans[0]["captions"]),
                    settings.intro_seconds + settings.countdown_seconds)]
    footage = {}
    for segment in run.timeline[2:]:
        number = segment.save_number
        if segment.kind == "save_intro":
            args, graph = title_scene(settings, number, segment.title, segment.duration)
            name = f"save_{number:02}_title"
        else:
            args, graph, footage[str(number)] = world_scene(
                renderer, store, settings, number, segment.title, segment.duration,
                len(run.source_render.collection.games[number - 1].broll_run_ids),
            )
            name = f"save_{number:02}_world"
            for item in footage[str(number)]:
                if item["kind"] == "still":
                    run.artifacts[item["source"]] = renderer._record(store, item["source"])
        paths.append(encode(name, args, graph, segment.duration))
    run.artifacts["footage_plan"] = store.write_text(
        "footage_plan.json", json.dumps(footage, indent=2), "application/json"
    )
    run.artifacts["concat"] = store.write_text(
        "segments.txt", "".join(f"file '{path}'\n" for path in paths)
    )
    (store.run_dir / "visuals.mp4").unlink(missing_ok=True)
    renderer._ffmpeg(store, "polished_visuals", ["-f", "concat", "-safe", "1", "-i",
                    "segments.txt", "-c:v", "copy", "-an", "visuals.mp4"])
    write_countdown_audio(store.run_dir / "countdown.wav", settings, total)
    run.artifacts["countdown"] = renderer._record(store, "countdown.wav")
    inputs, graph = ["-i", "visuals.mp4"], []
    for index, (cue, plan) in enumerate(zip(run.cues, plans, strict=True), 1):
        inputs += ["-i", cue.audio.path]
        graph.append(
            f"[{index}:a]atrim=start={plan['trim_start']}:end={plan['trim_end']},"
            f"asetpts=PTS-STARTPTS,atempo={plan['tempo']},aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={round(plan['offset'] * 1000)}:all=1[voice{index}]"
        )
    inputs += ["-i", "countdown.wav"]
    count = len(plans)
    # Rebuild timestamps from samples after mixing delayed, unequal-length cues.
    # Otherwise invalid amix timestamps can make atrim discard the spoken audio.
    graph.append("".join(f"[voice{i}]" for i in range(1, count + 1)) +
                 f"amix=inputs={count}:normalize=0,asetpts=N/SR/TB,"
                 f"apad=whole_dur={total},atrim=duration={total},"
                 "loudnorm=I=-16:TP=-2:LRA=7,aresample=48000[voice]")
    graph.append(f"[{count + 1}:a][voice]amix=inputs=2:duration=first:normalize=0,"
                 "alimiter=limit=0.95:level=false:latency=true[outa]")
    filters = []
    if run.settings.subtitles:
        for index, caption in enumerate(captions):
            # Opener/title cards already show their exact narration in the console UI.
            if caption["cue_id"].startswith("save_"):
                filters += caption_filter(store, run, settings, caption, index)
    graph.append("[0:v]" + (",".join(filters) or "null") + "[outv]")
    graph = ";\n".join(graph)
    run.artifacts["mix_filter"] = store.write_text("mix_filter.txt", graph)
    (store.run_dir / "reel.partial.mp4").unlink(missing_ok=True)
    renderer._ffmpeg(store, "narrated_reel", [*inputs, "-filter_complex", graph,
                    "-map", "[outv]", "-map", "[outa]", *codec, "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2", "-t", str(total), "-movflags", "+faststart",
                    "reel.partial.mp4"])
    info = probe_media(store.run_dir / "reel.partial.mp4", renderer.ffprobe)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if (abs(float(info["format"]["duration"]) - total) > 1 / settings.fps
            or int(video["nb_frames"]) != round(total * settings.fps)
            or (video["width"], video["height"]) != (settings.width, settings.height)
            or audio["codec_name"] != "aac"):
        raise MediaError("Polished reel failed duration, dimensions or audio checks")
    (store.run_dir / "reel.partial.mp4").replace(store.run_dir / "reel.mp4")
    run.artifacts["reel"] = renderer._record(store, "reel.mp4")
    run.artifacts["probe"] = store.write_text("probe.json", json.dumps(info, indent=2),
                                           "application/json")
    logger.info("Narrated reel completed: %s", store.run_dir / "reel.mp4")
