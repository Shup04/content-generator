"""Checkpointed execution of a single still-image to B-roll-video branch."""

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path

from save_reel.media_models import BrollRun, BrollSettings, BrollValues, MotionValues
from save_reel.models import StageState, StageStatus, utc_now
from save_reel.pipeline import run_logging
from save_reel.prompting import BrollStillPromptCompiler, BrollVideoPromptCompiler
from save_reel.providers.media import ImageProvider, MediaError, VideoProvider
from save_reel.storage import RunStore


class BrollPipeline:
    def __init__(
        self, image_provider: ImageProvider | None, video_provider: VideoProvider | None
    ) -> None:
        self.image_provider = image_provider
        self.video_provider = video_provider

    def prepare(
        self,
        values: BrollValues,
        motion: MotionValues,
        settings: BrollSettings,
        *,
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
        beat_number: int | None = None,
        prompts_dir: Path | None = None,
        still_template_version: str = "v2",
        video_template_version: str = "v2",
    ) -> RunStore:
        still_prompt = BrollStillPromptCompiler(
            still_template_version, prompts_dir=prompts_dir
        ).render(values.model_dump(by_alias=True))
        video_prompt = BrollVideoPromptCompiler(
            video_template_version, prompts_dir=prompts_dir
        ).render({**values.model_dump(by_alias=True), **motion.model_dump(by_alias=True)})
        if len(video_prompt.text) > 7000:
            raise MediaError("Compiled video prompt exceeds MiniMax's 7000-character limit")
        store = RunStore.create(runs_dir, run_id)
        run = BrollRun(
            run_id=store.run_dir.name,
            beat_number=beat_number,
            values=values,
            motion=motion,
            settings=settings,
            still_prompt=still_prompt,
            video_prompt=video_prompt,
        )
        run.artifacts["still_prompt"] = store.write_text(
            "broll_still_prompt.txt", still_prompt.text
        )
        run.artifacts["video_prompt"] = store.write_text(
            "broll_video_prompt.txt", video_prompt.text
        )
        self._save(store, run)
        return store

    @staticmethod
    def load(store: RunStore) -> BrollRun:
        run = BrollRun.model_validate_json(
            (store.run_dir / "broll.json").read_text(encoding="utf-8")
        )
        if run.run_id != store.run_dir.name:
            raise MediaError("B-roll run ID does not match its directory")
        return run

    @staticmethod
    def _save(store: RunStore, run: BrollRun) -> None:
        validated = BrollRun.model_validate(run)
        store.write_text(
            "broll.json",
            validated.model_dump_json(indent=2, by_alias=True) + "\n",
            "application/json",
        )

    @staticmethod
    def _read_artifact(store: RunStore, run: BrollRun, name: str) -> bytes:
        artifact = run.artifacts[name]
        content = (store.run_dir / artifact.path).read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise MediaError(
                f"Saved {name} checksum does not match; refusing to reuse modified media"
            )
        return content

    @staticmethod
    @contextmanager
    def _lock(store: RunStore):
        lock = store.run_dir / ".broll.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise MediaError(
                "This run is locked. If a previous process was killed, confirm it has stopped "
                "before removing .broll.lock and resuming."
            ) from None
        try:
            yield
        finally:
            lock.rmdir()

    def execute(
        self,
        store: RunStore,
        *,
        image_only: bool = False,
        wait_timeout: float = 900,
        poll_interval: float = 10,
    ) -> BrollRun:
        if wait_timeout <= 0 or not 0 < poll_interval <= 60:
            raise ValueError("wait timeout must be positive and poll interval must be in (0, 60]")
        with self._lock(store), run_logging(store) as logger:
            run = self.load(store)
            active_stage = "image_generation"
            logger.info("B-roll run %s: %s", run.run_id, run.values.game_title)
            try:
                run.status = StageStatus.RUNNING
                if "image" not in run.artifacts:
                    if run.image_attempted:
                        raise MediaError(
                            "An image request was already attempted without a saved image. "
                            "Check the previous error and provider before starting a new run."
                        )
                    if self.image_provider is None:
                        raise MediaError("An OpenAI image provider is required for this run")
                    run.stages[active_stage] = StageState(
                        status=StageStatus.RUNNING, started_at=utc_now()
                    )
                    run.image_attempted = True
                    self._save(store, run)
                    logger.info(
                        "Generating %s still at %s",
                        run.settings.image_model,
                        run.settings.image_size,
                    )
                    result = self.image_provider.generate(run.still_prompt.text, run.settings)
                    run.artifacts["image"] = store.write_bytes(
                        run.still_filename, result.content, "image/png"
                    )
                    run.image_request_id = result.request_id
                    run.image_usage = result.usage
                    run.stages[active_stage].status = StageStatus.COMPLETED
                    run.stages[active_stage].finished_at = utc_now()
                    self._save(store, run)
                    logger.info("Saved B-roll still: %s", store.run_dir / run.still_filename)
                image = self._read_artifact(store, run, "image")
                if image_only:
                    run.status = (
                        StageStatus.COMPLETED if "video" in run.artifacts else StageStatus.PENDING
                    )
                    self._save(store, run)
                    return run
                active_stage = "video_generation"
                if "video" in run.artifacts:
                    self._read_artifact(store, run, "video")
                    run.status = StageStatus.COMPLETED
                    self._save(store, run)
                    return run
                if self.video_provider is None:
                    raise MediaError("A MiniMax video provider is required to continue this run")
                run.stages[active_stage] = StageState(
                    status=StageStatus.RUNNING, started_at=utc_now()
                )
                if run.video_task_id is None:
                    if run.video_attempted:
                        raise MediaError(
                            "A MiniMax submission was attempted without a saved task ID. "
                            "Check your MiniMax tasks before starting a new run; "
                            "no duplicate was sent."
                        )
                    run.video_attempted = True
                    self._save(store, run)
                    logger.info("Submitting %s image-to-video task", run.settings.video_model)
                    run.video_task_id = self.video_provider.submit(
                        image, run.video_prompt.text, run.settings
                    )
                    # Save the task ID before any polling or downloading.
                    self._save(store, run)
                    logger.info("MiniMax task ID: %s", run.video_task_id)
                else:
                    logger.info("Resuming MiniMax task %s", run.video_task_id)
                deadline = time.monotonic() + wait_timeout
                while True:
                    task = self.video_provider.query(run.video_task_id)
                    run.video_task_status = task.status
                    run.video_usage = task.usage
                    self._save(store, run)
                    logger.info("MiniMax task status: %s", task.status)
                    if task.status in {"failed", "cancelled"}:
                        raise MediaError(f"MiniMax task {run.video_task_id} {task.status}")
                    if task.status == "succeeded":
                        content = self.video_provider.download(task.download_url)
                        run.artifacts["video"] = store.write_bytes(
                            run.video_filename, content, "video/mp4"
                        )
                        run.stages[active_stage].status = StageStatus.COMPLETED
                        run.stages[active_stage].finished_at = utc_now()
                        run.status = StageStatus.COMPLETED
                        self._save(store, run)
                        logger.info("Saved B-roll video: %s", store.run_dir / run.video_filename)
                        return run
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError(
                            "Video wait timed out. Resume this run to poll the existing task."
                        )
                    time.sleep(min(poll_interval, remaining))
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, MediaError)
                    else f"{type(exc).__name__} during {active_stage}"
                )
                run.status = StageStatus.FAILED
                stage = run.stages[active_stage]
                stage.status = StageStatus.FAILED
                stage.finished_at = utc_now()
                stage.error = message
                self._save(store, run)
                logger.error("%s", message)
                raise MediaError(message) from None
