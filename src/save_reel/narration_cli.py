"""Optional ElevenLabs command wiring; local cached speech can resume without a key."""

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from save_reel.narration import NarrationPipeline
from save_reel.narration_models import NarrationScript, SpeechSettings
from save_reel.providers.elevenlabs_speech import ElevenLabsSpeechProvider
from save_reel.providers.media import MediaError
from save_reel.storage import RunStore


def run_narration_command(args) -> Path:
    load_dotenv(args.env_file, override=False)
    store = RunStore(args.run_dir) if args.command == "resume-narration" else None
    existing = NarrationPipeline.load(store) if store else None
    needs_speech = existing is None or any(
        not (cue.audio and cue.metadata) and not cue.attempted for cue in existing.cues
    )
    if existing and any(
        cue.attempted and not (cue.audio and cue.metadata) for cue in existing.cues
    ):
        # Let the pipeline explain an ambiguous request before checking credentials.
        needs_speech = False
    if needs_speech and not os.getenv("ELEVENLABS_API_KEY", "").strip():
        raise MediaError(f"Missing ELEVENLABS_API_KEY. Set it locally in {args.env_file}")
    with httpx.Client(transport=httpx.HTTPTransport(retries=0)) as client:
        provider = (
            ElevenLabsSpeechProvider(os.environ["ELEVENLABS_API_KEY"], client)
            if needs_speech
            else None
        )
        pipeline = NarrationPipeline(provider, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        if store is None:
            script = NarrationScript.model_validate_json(args.script.read_text(encoding="utf-8"))
            voice_id = args.voice_id or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
            if not voice_id:
                voice_id, voice_name = provider.select_voice()
                logging.getLogger("save_reel").info("Using ElevenLabs voice: %s", voice_name)
            settings = SpeechSettings(
                voice_id=voice_id,
                model_id=args.model,
                speed=args.speed,
                subtitles=not args.no_subtitles,
            )
            store = pipeline.prepare(
                args.render_run, script, settings, runs_dir=args.runs_dir, run_id=args.run_id
            )
        return pipeline.execute(store)
