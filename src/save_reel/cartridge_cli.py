"""Image-only cartridge commands; no video provider or MiniMax key is needed."""

import os
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from save_reel.cartridge import CartridgePipeline
from save_reel.media_models import CartridgeValues, ImageSettings
from save_reel.providers.media import MediaError
from save_reel.providers.openai_image import OpenAIImageProvider
from save_reel.storage import RunStore


def run_cartridge_command(args) -> Path:
    load_dotenv(args.env_file, override=False)
    store = RunStore(args.run_dir) if args.command == "resume-cartridge" else None
    existing = CartridgePipeline.load(store) if store is not None else None
    needs_image = existing is None or "image" not in existing.artifacts
    if needs_image and not os.getenv("OPENAI_API_KEY", "").strip():
        raise MediaError(
            f"Missing OPENAI_API_KEY. Set locally in the environment or {args.env_file}."
        )
    with ExitStack() as stack:
        provider = None
        if needs_image:
            client = stack.enter_context(
                OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0, timeout=300)
            )
            provider = OpenAIImageProvider(client)
        pipeline = CartridgePipeline(provider)
        if store is None:
            values = CartridgeValues.model_validate_json(args.values.read_text(encoding="utf-8"))
            settings = ImageSettings(
                image_model=args.image_model,
                image_quality=args.image_quality,
                image_background=args.background,
            )
            store = pipeline.prepare(
                values, settings, runs_dir=args.runs_dir, run_id=args.run_id,
                template_version=args.template_version,
            )
        pipeline.execute(store)
        return store.run_dir / "cartridge.json"
