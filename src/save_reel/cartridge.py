"""Checkpointed single-cartridge generation using the shared image provider."""

import hashlib
from contextlib import contextmanager
from pathlib import Path

from save_reel.media_models import CartridgeRun, CartridgeValues, ImageSettings
from save_reel.models import StageState, StageStatus, utc_now
from save_reel.pipeline import run_logging
from save_reel.prompting import CartridgePromptCompiler
from save_reel.providers.media import ImageProvider, MediaError
from save_reel.storage import RunStore


class CartridgePipeline:
    def __init__(self, image_provider: ImageProvider | None) -> None:
        self.image_provider = image_provider

    def prepare(
        self,
        values: CartridgeValues,
        settings: ImageSettings,
        *,
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
        template_version: str = "v2",
        prompts_dir: Path | None = None,
    ) -> RunStore:
        if template_version == "v2" and settings.image_background != "transparent":
            raise MediaError("cartridge/v2 requires image_background=transparent")
        prompt = CartridgePromptCompiler(template_version, prompts_dir=prompts_dir).render(
            values.model_dump(by_alias=True, exclude_none=True)
        )
        store = RunStore.create(runs_dir, run_id)
        run = CartridgeRun(
            run_id=store.run_dir.name, values=values, settings=settings, prompt=prompt
        )
        run.artifacts["prompt"] = store.write_text("cartridge_prompt.txt", prompt.text)
        self._save(store, run)
        return store

    @staticmethod
    def load(store: RunStore) -> CartridgeRun:
        run = CartridgeRun.model_validate_json(
            (store.run_dir / "cartridge.json").read_text(encoding="utf-8")
        )
        if run.run_id != store.run_dir.name:
            raise MediaError("Cartridge run ID does not match its directory")
        return run

    @staticmethod
    def _save(store: RunStore, run: CartridgeRun) -> None:
        validated = CartridgeRun.model_validate(run)
        store.write_text(
            "cartridge.json",
            validated.model_dump_json(indent=2, by_alias=True) + "\n",
            "application/json",
        )

    @staticmethod
    @contextmanager
    def _lock(store: RunStore):
        lock = store.run_dir / ".cartridge.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise MediaError(
                "This run is locked. If a previous process was killed, confirm it has stopped "
                "before removing .cartridge.lock and resuming."
            ) from None
        try:
            yield
        finally:
            lock.rmdir()

    def execute(self, store: RunStore) -> CartridgeRun:
        with self._lock(store), run_logging(store) as logger:
            run = self.load(store)
            logger.info("Cartridge run %s: %s", run.run_id, run.values.title)
            try:
                if "image" in run.artifacts:
                    artifact = run.artifacts["image"]
                    content = (store.run_dir / artifact.path).read_bytes()
                    if hashlib.sha256(content).hexdigest() != artifact.sha256:
                        raise MediaError("Saved cartridge image checksum does not match")
                    logger.info("Reusing saved cartridge: %s", store.run_dir / artifact.path)
                else:
                    if run.image_attempted:
                        raise MediaError(
                            "A cartridge image request was already attempted without a saved "
                            "image. Check the previous error and provider before starting a new "
                            "run; no duplicate was sent."
                        )
                    if self.image_provider is None:
                        raise MediaError("An OpenAI image provider is required for this run")
                    run.status = StageStatus.RUNNING
                    run.stage = StageState(status=StageStatus.RUNNING, started_at=utc_now())
                    run.image_attempted = True
                    self._save(store, run)
                    logger.info(
                        "Generating %s cartridge at %s",
                        run.settings.image_model,
                        run.settings.image_size,
                    )
                    result = self.image_provider.generate(run.prompt.text, run.settings)
                    run.artifacts["image"] = store.write_bytes(
                        "cartridge.png", result.content, "image/png"
                    )
                    run.image_request_id = result.request_id
                    run.image_usage = result.usage
                    logger.info("Saved cartridge: %s", store.run_dir / "cartridge.png")
                run.stage.status = StageStatus.COMPLETED
                run.stage.error = None
                run.stage.finished_at = run.stage.finished_at or utc_now()
                run.status = StageStatus.COMPLETED
                self._save(store, run)
                return run
            except Exception as exc:
                message = str(exc) if isinstance(exc, MediaError) else type(exc).__name__
                run.status = StageStatus.FAILED
                run.stage.status = StageStatus.FAILED
                run.stage.finished_at = utc_now()
                run.stage.error = message
                self._save(store, run)
                logger.error("%s", message)
                raise MediaError(message) from None
