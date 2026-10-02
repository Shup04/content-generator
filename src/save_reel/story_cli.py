"""Story-only command assembly. No media providers are imported or invoked."""

import argparse
import logging
import os
from pathlib import Path

from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.story_export import review_text
from save_reel.story_history import StoryHistory
from save_reel.story_models import SAVE_IDS, StorySettings
from save_reel.story_pipeline import StoryWorkflow


def add_story_commands(commands):
    generate = commands.add_parser("generate-concepts", help="Generate and review stories only")
    generate.add_argument("--provider", choices=("mock", "openai"), default="mock")
    generate.add_argument("--count", type=int, default=1)
    generate.add_argument("--theme", default="strange worlds you must survive inside permanently")
    generate.add_argument("--settings", type=Path, help="StorySettings JSON overrides")
    generate.add_argument("--model", help="Overrides settings and SAVE_REEL_STORY_MODEL")
    generate.add_argument("--prompts-dir", type=Path)
    resume = commands.add_parser("resume-concepts", help="Reuse completed text stages in a run")
    resume.add_argument("run_dir", type=Path)
    regenerate = commands.add_parser(
        "regenerate-concepts", help="Fork cached stories, explicitly regenerating selected content"
    )
    regenerate.add_argument("run_dir", type=Path)
    regenerate.add_argument(
        "--scope", choices=("reel", "save", "candidates", "narration"), required=True
    )
    regenerate.add_argument("--save-id", choices=SAVE_IDS)
    regenerate.add_argument(
        "--narration-style", choices=("travelogue", "sol"),
        help="Upgrade one approved v3 world's writer; sol uses concise-description prose",
    )
    for command in (generate, regenerate):
        command.add_argument(
            "--runs-dir", type=Path, default=Path("runs") if command is generate else None
        )
        command.add_argument("--run-id", help="New run ID; with --count, becomes a numbered prefix")
        command.add_argument("--seed", type=int, help="Reproducible procedural seed")
    for command in (generate, resume, regenerate):
        command.add_argument(
            "--history-dir", type=Path, help="Shared history; default: RUNS_DIR/.story-history"
        )
        command.add_argument("--env-file", type=Path, default=Path(".env"))
        command.add_argument("--quiet", action="store_true", help="Print review paths only")
    review = commands.add_parser(
        "review-concepts", help="Print a cached story review, no API calls"
    )
    review.add_argument("run_dir", type=Path)
    history = commands.add_parser(
        "import-story-history", help="Merge accepted v1/v2 story runs into creative history"
    )
    history.add_argument(
        "sources", type=Path, nargs="+", help="Runs roots, run folders or manifests"
    )
    history.add_argument("--history-dir", type=Path, default=Path("runs/.story-history"))


def _load_env(env_file: Path):
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise ValueError("Install text dependencies: python -m pip install -e '.[story]'") from None
    load_dotenv(env_file, override=False)


def _provider(name: str, settings: StorySettings, env_file: Path):
    if name == "mock":
        return MockStoryProvider(version=settings.prompt_version)
    _load_env(env_file)
    try:
        from save_reel.providers.openai_story import OpenAIStoryProvider
    except ImportError:
        raise ValueError("Install text dependencies: python -m pip install -e '.[story]'") from None
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is missing; set it in the environment or local .env")
    return OpenAIStoryProvider(settings)


def run_story_command(args: argparse.Namespace) -> None:
    if args.command == "import-story-history":
        count, records = StoryHistory(args.history_dir).import_runs(tuple(args.sources))
        print(f"Imported {count} completed runs; {records} unique saves in {args.history_dir}")
        return
    if args.command == "review-concepts":
        print(review_text(StoryWorkflow.load(args.run_dir)))
        return
    history = StoryHistory(args.history_dir) if args.history_dir else None
    if args.command == "generate-concepts":
        if not 1 <= args.count <= 100:
            raise ValueError("--count must be between 1 and 100")
        if args.provider == "openai":
            _load_env(args.env_file)
        else:
            logging.getLogger("save_reel").warning(
                "Mock fixtures only: use --provider openai to assess Luna writing. "
                "Repeated fixture worlds will be rejected by novelty checks."
            )
        settings = (
            StorySettings.model_validate_json(args.settings.read_text(encoding="utf-8"))
            if args.settings
            else StorySettings()
        )
        model = args.model or os.environ.get("SAVE_REEL_STORY_MODEL")
        if model:
            settings.model = model
        workflow = StoryWorkflow(_provider(args.provider, settings, args.env_file), history=history)
        for index in range(args.count):
            run_id = args.run_id
            if args.count > 1 and run_id:
                run_id = f"{run_id}-{index + 1:03d}"
            run = workflow.create(
                ConceptRequest(theme=args.theme),
                settings=settings,
                runs_dir=args.runs_dir,
                run_id=run_id,
                random_seed=None if args.seed is None else args.seed + index,
                prompts_dir=args.prompts_dir,
            )
            if not args.quiet:
                print(review_text(run))
            print(args.runs_dir / run.run_id / "story_review.txt")
        return
    saved = StoryWorkflow.load(args.run_dir)
    complete = all(s.final for s in saved.saves) and not saved.narration_pending
    provider = (
        MockStoryProvider()
        if complete and args.command == "resume-concepts"
        else _provider(saved.provider, saved.settings, args.env_file)
    )
    workflow = StoryWorkflow(provider, history=history)
    if args.command == "resume-concepts":
        run = workflow.resume(args.run_dir)
        root = args.run_dir.parent
    else:
        run = workflow.regenerate(
            args.run_dir,
            scope=args.scope,
            save_id=args.save_id,
            runs_dir=args.runs_dir,
            run_id=args.run_id,
            random_seed=args.seed,
            narration_style=args.narration_style,
        )
        root = args.runs_dir or args.run_dir.parent
    if not args.quiet:
        print(review_text(run))
    print(root / run.run_id / "story_review.txt")
