"""Command-line entry point; dependencies are assembled here."""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from save_reel.models import ConceptRequest
from save_reel.pipeline import CreatePipeline
from save_reel.prompting import BrollStillPromptCompiler, LabelPromptCompiler
from save_reel.providers import MockConceptProvider
from save_reel.providers.media import MediaError
from save_reel.stages import BrollStillPromptStage, LabelPromptStage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="save-reel", description="Create Choose Your Save reels")
    commands = parser.add_subparsers(dest="command", required=True)
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
    generate.add_argument("--duration", type=int, choices=range(4, 16), default=5)
    generate.add_argument("--resolution", choices=("768P", "2K"), default="768P")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Provider libraries may otherwise log signed download URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
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
