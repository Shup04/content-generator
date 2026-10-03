"""Command-line entry point; dependencies are assembled here."""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from save_reel.media_models import BrollSettings
from save_reel.models import ConceptRequest
from save_reel.pipeline import CreatePipeline
from save_reel.prompting import BrollStillPromptCompiler, LabelPromptCompiler
from save_reel.providers import MockConceptProvider
from save_reel.providers.media import MediaError
from save_reel.stages import BrollStillPromptStage, LabelPromptStage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="save-reel", description="Create Choose Your Save reels")
    commands = parser.add_subparsers(dest="command", required=True)
    from save_reel.story_cli import add_story_commands

    add_story_commands(commands)
    from save_reel.studio_cli import add_studio_command

    add_studio_command(commands)
    create = commands.add_parser("create", help="Prepare label and B-roll prompts for four games")
    create.add_argument("--theme", default="forgotten worlds", help="Creative theme for the reel")
    create.add_argument("--provider", choices=("mock",), default="mock")
    create.add_argument("--runs-dir", type=Path, default=Path("runs"))
    create.add_argument("--run-id", help="Unique run ID (letters, digits, underscores, hyphens)")
    create.add_argument(
        "--template-version", default="v1", help="Default version for both templates"
    )
    create.add_argument("--label-template-version", help="Override the label template version")
    create.add_argument(
        "--broll-template-version", help="Override the B-roll still template version"
    )
    create.add_argument(
        "--prompts-dir", type=Path, help="Override the bundled prompt template root"
    )
    generate = commands.add_parser(
        "generate-broll", help="Generate one still and a MiniMax H3 video (paid APIs)"
    )
    generate.add_argument(
        "--values", type=Path, required=True, help="Six image prompt values in JSON"
    )
    generate.add_argument(
        "--motion-values",
        type=Path,
        required=True,
        help="Camera, environment motion, and audio in JSON",
    )
    generate.add_argument("--runs-dir", type=Path, default=Path("runs"))
    generate.add_argument("--run-id")
    generate.add_argument(
        "--still-template-version", default="v2", help="B-roll still template version (default: v2)"
    )
    generate.add_argument(
        "--video-template-version", default="v2", help="B-roll video template version (default: v2)"
    )
    generate.add_argument(
        "--image-model",
        choices=("gpt-image-2.5-sunburst", "gpt-image-2.5-flare"),
        default="gpt-image-2.5-sunburst",
    )
    generate.add_argument(
        "--image-quality", choices=("low", "medium", "high", "xhigh", "max"), default="medium"
    )
    video_defaults = BrollSettings()
    generate.add_argument("--duration", type=int, choices=range(4, 16),
                          default=video_defaults.duration)
    generate.add_argument("--resolution", choices=("480P", "768P", "2K"),
                          default=video_defaults.resolution)
    generate.add_argument(
        "--video-model", choices=("MiniMax-H3", "MiniMax-H3-Max"),
        default=video_defaults.video_model
    )
    resume = commands.add_parser(
        "resume-broll", help="Continue a saved B-roll run without regenerating completed media"
    )
    resume.add_argument("run_dir", type=Path)
    for command in (generate, resume):
        command.add_argument("--env-file", type=Path, default=Path(".env"))
        command.add_argument(
            "--image-only",
            action="store_true",
            help="Stop after the still image; resume for video later",
        )
        command.add_argument(
            "--wait-timeout",
            type=float,
            default=900,
            help="Seconds to wait for video before saving and exiting",
        )
    cartridge = commands.add_parser(
        "generate-cartridge", help="Generate one cartridge image (paid OpenAI API)"
    )
    cartridge.add_argument("--values", type=Path, required=True, help="Cartridge values in JSON")
    cartridge.add_argument("--runs-dir", type=Path, default=Path("runs"))
    cartridge.add_argument("--run-id")
    cartridge.add_argument("--template-version", default="v2")
    cartridge.add_argument(
        "--background", choices=("transparent", "opaque"), default="transparent",
        help="Image background; cartridge/v2 requires transparent",
    )
    cartridge.add_argument(
        "--image-model",
        choices=("gpt-image-2.5-sunburst", "gpt-image-2.5-flare"),
        default="gpt-image-2.5-sunburst",
    )
    cartridge.add_argument(
        "--image-quality", choices=("low", "medium", "high", "xhigh", "max"), default="medium"
    )
    resume_cartridge = commands.add_parser(
        "resume-cartridge", help="Continue a saved cartridge run without regenerating its image"
    )
    resume_cartridge.add_argument("run_dir", type=Path)
    for command in (cartridge, resume_cartridge):
        command.add_argument("--env-file", type=Path, default=Path(".env"))
    render = commands.add_parser("render", help="Assemble saved cartridges and B-roll with FFmpeg")
    render.add_argument("--collection", type=Path, required=True)
    render.add_argument(
        "--settings", type=Path, help="Render settings JSON; defaults to the first pass"
    )
    render.add_argument("--source-runs-dir", type=Path, default=Path("runs"))
    render.add_argument("--runs-dir", type=Path, default=Path("runs"))
    render.add_argument("--run-id")
    render.add_argument("--font-file", type=Path)
    render.add_argument("--ffmpeg", default="ffmpeg")
    render.add_argument("--ffprobe", default="ffprobe")
    narrate = commands.add_parser("narrate", help="Add ElevenLabs narration to a saved reel")
    narrate.add_argument("--render-run", type=Path, required=True)
    narrate.add_argument("--script", type=Path, required=True)
    narrate.add_argument("--runs-dir", type=Path, default=Path("runs"))
    narrate.add_argument("--run-id")
    narrate.add_argument("--voice-id", help="Overrides ELEVENLABS_VOICE_ID; otherwise auto-select")
    narrate.add_argument("--model", default="eleven_multilingual_v2")
    narrate.add_argument("--speed", type=float, default=1.0)
    narrate.add_argument("--no-subtitles", action="store_true")
    narrate.add_argument("--reuse-speech-run", type=Path,
                        help="Reuse matching recordings from a previous narration run")
    resume_narration = commands.add_parser(
        "resume-narration", help="Reuse saved speech and finish narration assembly"
    )
    resume_narration.add_argument("run_dir", type=Path)
    for command in (narrate, resume_narration):
        command.add_argument("--env-file", type=Path, default=Path(".env"))
        command.add_argument("--ffmpeg", default="ffmpeg")
        command.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Provider libraries may otherwise log signed download URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "studio":
            from save_reel.studio_cli import run_studio_command

            run_studio_command(args)
            return 0
        if args.command in {"generate-concepts", "resume-concepts", "regenerate-concepts",
                            "review-concepts", "import-story-history"}:
            from save_reel.story_cli import run_story_command

            try:
                run_story_command(args)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            return 0
        if args.command in {"narrate", "resume-narration"}:
            try:
                from save_reel.narration_cli import run_narration_command
            except ImportError:
                raise MediaError(
                    "Install speech dependencies with: python -m pip install -e '.[speech]'"
                ) from None
            print(run_narration_command(args))
            return 0
        if args.command == "render":
            from save_reel.render_models import RenderCollection, RenderSettings
            from save_reel.rendering import ReelRenderer

            collection = RenderCollection.model_validate_json(args.collection.read_text())
            settings = (
                RenderSettings.model_validate_json(args.settings.read_text())
                if args.settings else RenderSettings()
            )
            output = ReelRenderer(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe).render(
                collection, settings, source_runs_dir=args.source_runs_dir,
                runs_dir=args.runs_dir, run_id=args.run_id, font_file=args.font_file,
            )
            print(output)
            return 0
        if args.command in {"generate-cartridge", "resume-cartridge"}:
            try:
                from save_reel.cartridge_cli import run_cartridge_command
            except ImportError:
                raise MediaError(
                    "Install media dependencies with: python -m pip install -e '.[media]'"
                ) from None
            print(run_cartridge_command(args))
            return 0
        if args.command in {"generate-broll", "resume-broll"}:
            try:
                from save_reel.media_cli import run_media_command
            except ImportError:
                raise MediaError(
                    "Install media dependencies with: python -m pip install -e '.[media]'"
                ) from None
            print(run_media_command(args))
            return 0
        request = ConceptRequest(theme=args.theme)
        label_compiler = LabelPromptCompiler(
            args.label_template_version or args.template_version, prompts_dir=args.prompts_dir
        )
        broll_compiler = BrollStillPromptCompiler(
            args.broll_template_version or args.template_version, prompts_dir=args.prompts_dir
        )
        pipeline = CreatePipeline(
            MockConceptProvider(),
            stages=(LabelPromptStage(label_compiler), BrollStillPromptStage(broll_compiler)),
        )
        reel = pipeline.create(request, runs_dir=args.runs_dir, run_id=args.run_id)
    except (ValueError, OSError, MediaError) as exc:
        logging.getLogger("save_reel").error("%s failed: %s", args.command.capitalize(), exc)
        return 1
    print(args.runs_dir / reel.run_id / "manifest.json")
    return 0
