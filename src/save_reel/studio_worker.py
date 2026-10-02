"""Reusable production glue, using existing stage APIs and their paid-request guards."""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.concurrency import parallel_each
from save_reel.models import ConceptRequest
from save_reel.narration_models import NarrationGame, NarrationScript
from save_reel.opener_style import INTRO_SCRIPT
from save_reel.render_models import RenderCollection, RenderGame, RenderRun
from save_reel.storage import RunStore
from save_reel.story_pipeline import StoryWorkflow
from save_reel.studio_jobs import timestamp
from save_reel.studio_models import JobRequest, StudioDraft
from save_reel.studio_progress import LABELS, plan, update
from save_reel.studio_store import StudioStore, compile_game, games_from_story, write_json


class ProductionJob:
    def __init__(self, store: StudioStore, job_id: str):
        self.store = store
        self.folder = store.jobs / job_id
        self.job = json.loads((self.folder / "job.json").read_text())
        self.request = JobRequest.model_validate(self.job["request"])
        self.draft = StudioDraft.model_validate_json((self.folder / "draft.json").read_text())
        self.prompts = self.folder / "prompts"
        self.job_id = job_id
        self.lock = threading.RLock()
        self.job.setdefault("progress", plan(self.request, self.draft))

    def checkpoint(self, phase=None):
        with self.lock:
            self._checkpoint(phase)

    def progress(self, stage, unit, status):
        with self.lock:
            update(self.job["progress"], stage, unit, status)
            if status == "running":
                self.job["phase"] = LABELS.get(stage, stage)
            self.checkpoint()

    def completed_stage(self, stage):
        row = next((s for s in self.job["progress"]["stages"] if s["id"] == stage), None)
        if row:
            for unit in row["units"]:
                self.progress(stage, unit, "completed")

    def _checkpoint(self, phase=None):
        if phase:
            self.job["phase"] = phase
            logging.info(phase)
        write_json(self.folder / "draft.json", self.draft.model_dump(mode="json"))
        # The web server locks this draft while its worker runs.
        write_json(
            self.store.drafts / f"{self.draft.draft_id}.json", self.draft.model_dump(mode="json")
        )
        # Publish a terminal job status only after both editable state and snapshot are saved.
        write_json(self.folder / "job.json", self.job)

    def command(self, *args):
        from save_reel.cli import main

        if main(list(map(str, args))) != 0:
            raise ValueError(
                "Stage failed. Read the job log for details; saved results are retained."
            )

    def stories(self):
        from save_reel.story_cli import _provider
        from save_reel.story_history import StoryHistory

        self.checkpoint("Writing and reviewing four stories")
        run_id = self.job_id + "-story"
        target = self.store.run_dir(run_id)
        # Mock history is isolated so repeat UI tests do not consume creative territory.
        history = (
            StoryHistory(self.folder / "mock-history") if self.draft.provider == "mock" else None
        )
        provider = _provider(self.draft.provider, self.draft.story, self.store.project / ".env")
        workflow = StoryWorkflow(
            provider, history=history, max_workers=self.draft.parallel_saves, progress=self.progress
        )
        if (target / "story.json").exists():
            run = workflow.resume(target)
        else:
            run = workflow.create(
                ConceptRequest(theme=self.draft.theme),
                settings=self.draft.story,
                runs_dir=self.store.runs,
                run_id=run_id,
                random_seed=self.draft.seed,
                prompts_dir=self.prompts,
            )
        self.draft.games = games_from_story(run)
        self.draft.source_story = run_id
        self.draft.last_render = self.draft.last_narration = None
        self.job["outputs"]["story"] = run_id
        for stage in ("concepts", "diversity", "worlds", "scripts"):
            self.completed_stage(stage)
        self.checkpoint()

    def media(self, kind):
        targets = [
            (i, g)
            for i, g in enumerate(self.draft.games, 1)
            if not self.request.save_number or i == self.request.save_number
        ]

        def work(item):
            number, game = item
            unit = f"save_{number:02}"
            self.progress(kind, unit, "running")
            try:
                self.media_save(kind, number, game)
            except Exception:
                self.progress(kind, unit, "failed")
                raise
            self.progress(kind, unit, "completed")

        parallel_each(work, targets, self.draft.parallel_saves)

    def reuse_still(self, saved, reference, target, compiled):
        if self.request.regenerate and self.request.action == "stills":
            return
        fields = {"image_model", "image_size", "image_quality", "image_background"}
        if (
            "image" not in saved.artifacts
            or saved.still_prompt != compiled["still"]
            or saved.settings.model_dump(include=fields)
            != self.draft.broll.model_dump(include=fields)
        ):
            return
        source = RunStore(self.store.run_dir(reference))
        content = BrollPipeline._read_artifact(source, saved, "image")
        destination = RunStore(target)
        run = BrollPipeline.load(destination)
        run.artifacts["image"] = destination.write_bytes("broll_still.png", content, "image/png")
        run.image_attempted = True
        run.image_request_id = saved.image_request_id
        run.image_usage = saved.image_usage
        run.stages["image_generation"] = saved.stages["image_generation"].model_copy(deep=True)
        run.reused_still_run_id = reference
        BrollPipeline._save(destination, run)

    def media_save(self, kind, number, game):
        compiled = compile_game(self.draft, game, self.prompts)
        is_cart = kind == "cartridges"
        suffix = f"cart{number:02}" if is_cart else f"broll{number:02}"
        planned = self.job_id + "-" + suffix
        reference = game.cartridge_run if is_cart else game.broll_run
        pipeline = CartridgePipeline(None) if is_cart else BrollPipeline(None, None)
        reuse = None
        saved = None
        if reference:
            saved = pipeline.load(RunStore(self.store.run_dir(reference)))
            compatible = (
                saved.values == game.cartridge
                and saved.settings == self.draft.cartridge
                and saved.prompt == compiled["cartridge"]
                if is_cart
                else saved.values == game.environment
                and saved.motion == game.motion
                and saved.settings == self.draft.broll
                and saved.still_prompt == compiled["still"]
                and saved.video_prompt == compiled["video"]
            )
            if compatible and (not self.request.regenerate or reference == planned):
                reuse = reference
        run_id = reuse or planned
        target = self.store.run_dir(run_id)
        if not target.exists():
            if is_cart:
                pipeline.prepare(
                    game.cartridge,
                    self.draft.cartridge,
                    runs_dir=self.store.runs,
                    run_id=run_id,
                    template_version=self.draft.cartridge_version,
                    prompts_dir=self.prompts,
                )
            else:
                pipeline.prepare(
                    game.environment,
                    game.motion,
                    self.draft.broll,
                    runs_dir=self.store.runs,
                    run_id=run_id,
                    still_template_version=self.draft.still_version,
                    video_template_version=self.draft.video_version,
                    prompts_dir=self.prompts,
                )
        if not is_cart and saved is not None and reference != run_id:
            prepared = pipeline.load(RunStore(target))
            if "image" not in prepared.artifacts and not prepared.image_attempted:
                self.reuse_still(saved, reference, target, compiled)
        if is_cart:
            game.cartridge_run = run_id
        else:
            game.broll_run = run_id
        self.checkpoint()
        command = "resume-cartridge" if is_cart else "resume-broll"
        extra = ["--image-only"] if kind == "stills" else []
        self.command(command, target, "--env-file", self.store.project / ".env", *extra)
        if kind == "videos" and "video" not in pipeline.load(RunStore(target)).artifacts:
            raise ValueError("MiniMax is still processing. Resume this job to poll the same task.")

    def render(self):
        from save_reel.rendering import ReelRenderer

        self.checkpoint("Rendering the reel with FFmpeg")
        games = []
        for number, game in enumerate(self.draft.games, 1):
            if not game.cartridge_run or not game.broll_run:
                raise ValueError(f"Save {number:02} needs a cartridge and B-roll clip first")
            cart = CartridgePipeline.load(RunStore(self.store.run_dir(game.cartridge_run)))
            roll = BrollPipeline.load(RunStore(self.store.run_dir(game.broll_run)))
            compiled = compile_game(self.draft, game, self.prompts)
            if (
                cart.values != game.cartridge
                or roll.values != game.environment
                or roll.motion != game.motion
                or cart.prompt != compiled["cartridge"]
                or roll.still_prompt != compiled["still"]
                or roll.video_prompt != compiled["video"]
            ):
                raise ValueError(
                    f"Save {number:02} media no longer matches its variables/prompts. "
                    "Generate the changed media stage first."
                )
            values_file = self.folder / f"cartridge-{number:02}.json"
            write_json(values_file, game.cartridge.model_dump(mode="json", by_alias=True))
            games.append(
                RenderGame(
                    title=game.title,
                    values_file=str(values_file),
                    cartridge_run_id=game.cartridge_run,
                    broll_run_ids=(game.broll_run,),
                )
            )
        collection = RenderCollection(
            description=self.draft.name,
            template_version=self.draft.cartridge_version,
            image_background="transparent",
            games=games,
        )
        run_id = self.job_id + "-render"
        target = self.store.run_dir(run_id)
        if target.exists():
            saved = RenderRun.model_validate_json((target / "render.json").read_text())
            if saved.status != "completed":
                # Renderer has no resume API. Keep the failed output for inspection.
                run_id += "-" + str(int(time.time()))
                target = self.store.run_dir(run_id)
        if not target.exists():
            ReelRenderer().render(
                collection,
                self.draft.render,
                source_runs_dir=self.store.runs,
                runs_dir=self.store.runs,
                run_id=run_id,
            )
        self.draft.last_render = run_id
        self.job["outputs"]["render"] = run_id
        self.checkpoint()

    def narrate(self):
        import httpx
        from dotenv import load_dotenv

        from save_reel.narration import NarrationPipeline
        from save_reel.providers.elevenlabs_speech import ElevenLabsSpeechProvider

        self.checkpoint("Recording narration and adding subtitles")
        load_dotenv(self.store.project / ".env", override=False)
        run_id = self.job_id + "-reel"
        target = self.store.run_dir(run_id)
        with httpx.Client(transport=httpx.HTTPTransport(retries=0)) as client:
            key = os.getenv("ELEVENLABS_API_KEY", "")
            pipeline = NarrationPipeline(ElevenLabsSpeechProvider(key, client) if key else None)
            if not target.exists():
                script = NarrationScript(
                    intro=INTRO_SCRIPT,
                    games=tuple(
                        NarrationGame(title=g.title, text=g.narration) for g in self.draft.games
                    ),
                )
                reuse = None
                if self.draft.last_narration:
                    candidate = self.store.run_dir(self.draft.last_narration)
                    old = pipeline.load(RunStore(candidate))
                    fields = {"voice_id", "model_id", "speed", "stability", "similarity_boost"}
                    if old.settings.model_dump(include=fields) == self.draft.speech.model_dump(
                        include=fields
                    ):
                        reuse = candidate
                pipeline.prepare(
                    self.store.run_dir(self.draft.last_render),
                    script,
                    self.draft.speech,
                    runs_dir=self.store.runs,
                    run_id=run_id,
                    reuse_speech_dir=reuse,
                )
            saved = pipeline.load(RunStore(target))
            if not key and any(not c.audio and not c.attempted for c in saved.cues):
                raise ValueError("ELEVENLABS_API_KEY is missing. Add it to the local .env file.")
            self.draft.last_narration = run_id
            self.checkpoint()
            pipeline.execute(
                RunStore(target), max_workers=self.draft.parallel_saves, progress=self.progress
            )
        for stage in ("speech", "compose"):
            self.completed_stage(stage)
        self.job["outputs"]["reel"] = run_id
        self.checkpoint()

    def execute(self):
        self.job.update(status="running", error=None)
        self.checkpoint()
        try:
            action = self.request.action
            if action == "stories" or (
                action == "full"
                and (
                    not self.draft.games
                    or (self.request.new_stories and not self.job["outputs"].get("story"))
                )
            ):
                self.stories()
            if action in ("cartridges", "full"):
                self.media("cartridges")
            if action in ("stills", "videos", "full"):
                self.media("stills")
            if action in ("videos", "full"):
                self.media("videos")
            if action in ("render", "narrate", "full"):
                self.progress("render", "reel", "running")
                self.render()
                self.progress("render", "reel", "completed")
            if action in ("narrate", "full"):
                self.narrate()
                from save_reel.studio_store import is_finished_reel

                final_id = self.job["outputs"].get("reel")
                if not final_id or not is_finished_reel(self.store.run_dir(final_id)):
                    raise ValueError("Final narrated MP4 is missing or incomplete. Check Activity.")
            self.job.update(status="completed", finished_at=timestamp(), phase="Complete")
        except Exception as exc:
            # Provider adapters already redact errors; never serialize exception responses.
            message = (
                str(exc)
                if isinstance(exc, (ValueError, OSError, RuntimeError))
                else type(exc).__name__
            )
            for name in ("OPENAI_API_KEY", "MINIMAX_API_KEY", "ELEVENLABS_API_KEY"):
                if os.getenv(name):
                    message = message.replace(os.environ[name], "[redacted]")
            for stage in self.job["progress"]["stages"]:
                for unit, state in stage["units"].items():
                    if state == "running":
                        stage["units"][unit] = "failed"
            self.job.update(status="failed", error=message, finished_at=timestamp())
            logging.error("%s", message)
        self.draft.revision += 1
        self.checkpoint()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store, job_id = StudioStore(Path(sys.argv[1])), sys.argv[2]
    for _ in range(100):
        job = json.loads((store.jobs / job_id / "job.json").read_text())
        if job.get("pid") == os.getpid():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("Worker startup handshake failed")
    ProductionJob(store, job_id).execute()


if __name__ == "__main__":
    main()
