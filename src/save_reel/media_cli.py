"""Optional real API commands; importing the mock CLI needs no media dependencies."""

import os
from contextlib import ExitStack
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from save_reel.broll import BrollPipeline
from save_reel.media_models import BrollSettings, BrollValues, MotionValues
from save_reel.providers.media import MediaError
from save_reel.providers.minimax_video import MiniMaxVideoProvider
from save_reel.providers.openai_image import OpenAIImageProvider
from save_reel.storage import RunStore


def run_media_command(args) -> Path:
    load_dotenv(args.env_file, override=False)
    store = RunStore(args.run_dir) if args.command == "resume-broll" else None
    existing = BrollPipeline.load(store) if store is not None else None
    needs_image = existing is None or "image" not in existing.artifacts
    needs_video = not args.image_only and (existing is None or "video" not in existing.artifacts)
    required = []
    if needs_image:
        required.append("OPENAI_API_KEY")
    if needs_video:
        required.append("MINIMAX_API_KEY")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise MediaError(
            f"Missing {', '.join(missing)}. Set locally in the environment or {args.env_file}."
        )
    # Credential checks precede creating a run or sending a paid request.
    with ExitStack() as stack:
        image_provider = None
        video_provider = None
        if needs_image:
            client = stack.enter_context(
                OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0, timeout=300)
            )
            image_provider = OpenAIImageProvider(client)
        if needs_video:
            client = stack.enter_context(httpx.Client())
            video_provider = MiniMaxVideoProvider(os.environ["MINIMAX_API_KEY"], client)
        pipeline = BrollPipeline(image_provider, video_provider)
        if store is None:
            values = BrollValues.model_validate_json(args.values.read_text(encoding="utf-8"))
            motion = MotionValues.model_validate_json(
                args.motion_values.read_text(encoding="utf-8")
            )
            settings = BrollSettings(
                image_model=args.image_model,
                image_quality=args.image_quality,
                duration=args.duration,
                resolution=args.resolution,
                video_model=args.video_model,
            )
            store = pipeline.prepare(
                values,
                motion,
                settings,
                runs_dir=args.runs_dir,
                run_id=args.run_id,
                still_template_version=args.still_template_version,
                video_template_version=args.video_template_version,
            )
        pipeline.execute(store, image_only=args.image_only, wait_timeout=args.wait_timeout)
        return store.run_dir / "broll.json"
