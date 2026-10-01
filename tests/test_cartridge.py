from pathlib import Path

import pytest

from save_reel.cartridge import CartridgePipeline
from save_reel.cli import main
from save_reel.media_models import CartridgeRun, CartridgeValues, ImageSettings
from save_reel.models import StageStatus
from save_reel.providers.media import GeneratedImage, MediaError
from save_reel.storage import RunStore


@pytest.fixture
def cartridge_values():
    path = Path(__file__).resolve().parents[1] / "examples/cartridges/v2/deep-end.json"
    return CartridgeValues.model_validate_json(path.read_text())


@pytest.fixture
def cartridge_settings():
    return ImageSettings(image_background="transparent")


class FakeImage:
    calls = 0
    store = None
    fail = False

    def generate(self, prompt, settings):
        self.calls += 1
        if self.store is not None:
            checkpoint = CartridgePipeline.load(self.store)
            assert checkpoint.image_attempted
            assert checkpoint.stage.status == StageStatus.RUNNING
        if self.fail:
            raise MediaError("Image request timed out")
        return GeneratedImage(b"test image bytes", request_id="req_cartridge")


def test_cartridge_checkpoints_and_completed_resume_is_idempotent(
    tmp_path, cartridge_values, cartridge_settings
):
    image = FakeImage()
    pipeline = CartridgePipeline(image)
    store = pipeline.prepare(cartridge_values, cartridge_settings, runs_dir=tmp_path)
    image.store = store
    pending = pipeline.load(store)
    assert pending.prompt.template.name == "cartridge"
    assert pending.prompt.template.version == "v2"
    assert "[TITLE]" not in pending.prompt.text
    assert store.run_dir.joinpath("cartridge_prompt.txt").read_text() == pending.prompt.text
    run = pipeline.execute(store)
    assert run.status == StageStatus.COMPLETED
    assert run.stage.status == StageStatus.COMPLETED
    assert run.image_request_id == "req_cartridge"
    assert store.run_dir.joinpath("cartridge.png").read_bytes() == b"test image bytes"
    assert pipeline.load(store) == run
    assert CartridgePipeline(None).execute(store) == run
    assert image.calls == 1
    assert not store.run_dir.joinpath(".cartridge.lock").exists()
    assert not store.run_dir.joinpath("broll.json").exists()


def test_cartridge_ambiguous_failure_is_not_retried(tmp_path, cartridge_values, cartridge_settings):
    image = FakeImage()
    image.fail = True
    pipeline = CartridgePipeline(image)
    store = pipeline.prepare(cartridge_values, cartridge_settings, runs_dir=tmp_path)
    with pytest.raises(MediaError, match="timed out"):
        pipeline.execute(store)
    assert pipeline.load(store).status == StageStatus.FAILED
    with pytest.raises(MediaError, match="no duplicate"):
        pipeline.execute(store)
    assert image.calls == 1


def test_cartridge_detects_corruption_without_regenerating(
    tmp_path, cartridge_values, cartridge_settings
):
    image = FakeImage()
    pipeline = CartridgePipeline(image)
    store = pipeline.prepare(cartridge_values, cartridge_settings, runs_dir=tmp_path)
    pipeline.execute(store)
    store.run_dir.joinpath("cartridge.png").write_bytes(b"corrupt")
    with pytest.raises(MediaError, match="checksum"):
        pipeline.execute(store)
    assert image.calls == 1


def test_cartridge_lock_blocks_concurrent_execution(tmp_path, cartridge_values, cartridge_settings):
    image = FakeImage()
    pipeline = CartridgePipeline(image)
    store = pipeline.prepare(cartridge_values, cartridge_settings, runs_dir=tmp_path)
    lock = store.run_dir / ".cartridge.lock"
    lock.mkdir()
    with pytest.raises(MediaError, match="locked"):
        pipeline.execute(store)
    assert lock.exists()
    assert image.calls == 0


def test_cartridge_cli_requires_only_openai_and_completed_resume_needs_no_keys(
    tmp_path, monkeypatch, cartridge_values
):
    pytest.importorskip("openai")
    pytest.importorskip("dotenv")
    pytest.importorskip("PIL")
    from save_reel import cartridge_cli

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    image = FakeImage()
    monkeypatch.setattr(cartridge_cli, "OpenAIImageProvider", lambda client: image)
    values_file = tmp_path / "values.json"
    values_file.write_text(cartridge_values.model_dump_json(by_alias=True))
    env = ["--env-file", str(tmp_path / "absent.env")]
    assert main([
        "generate-cartridge", "--values", str(values_file), "--runs-dir", str(tmp_path),
        "--run-id", "cartridge", *env,
    ]) == 0
    store = RunStore(tmp_path / "cartridge")
    run = CartridgePipeline.load(store)
    assert run.status == StageStatus.COMPLETED
    assert run.settings.image_background == "transparent"
    assert run.prompt.template.version == "v2"
    assert cartridge_values.hint_scene_description in run.prompt.text
    monkeypatch.delenv("OPENAI_API_KEY")
    assert main(["resume-cartridge", str(store.run_dir), *env]) == 0
    assert image.calls == 1
    assert main([
        "generate-cartridge", "--values", str(values_file), "--runs-dir", str(tmp_path),
        "--run-id", "missing-key", *env,
    ]) == 1
    assert not (tmp_path / "missing-key").exists()


def test_cartridge_v2_requires_transparent_setting_before_creating_run(tmp_path, cartridge_values):
    with pytest.raises(MediaError, match="requires image_background=transparent"):
        CartridgePipeline(None).prepare(cartridge_values, ImageSettings(), runs_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_v1_cartridge_inputs_and_old_state_keep_their_original_meaning(tmp_path):
    values_path = Path(__file__).resolve().parents[1] / "examples/cartridges/deep-end.json"
    values = CartridgeValues.model_validate_json(values_path.read_text())
    pipeline = CartridgePipeline(FakeImage())
    store = pipeline.prepare(values, ImageSettings(), template_version="v1", runs_dir=tmp_path)
    run = pipeline.execute(store)
    old_data = run.model_dump(by_alias=True)
    old_data["settings"].pop("image_background")
    old_data["values"].pop("HINT_SCENE_DESCRIPTION")
    restored = CartridgeRun.model_validate(old_data)
    assert restored.settings.image_background == "opaque"
    assert restored.values.hint_scene_description is None
    assert restored.prompt == run.prompt
    assert restored.prompt.template.version == "v1"
    assert values.label_scene_description in restored.prompt.text
    assert CartridgePipeline(None).execute(store) == run


def test_full_scene_values_are_not_silently_used_as_teasers(tmp_path):
    values_path = Path(__file__).resolve().parents[1] / "examples/cartridges/deep-end.json"
    values = CartridgeValues.model_validate_json(values_path.read_text())
    with pytest.raises(ValueError, match="HINT_SCENE_DESCRIPTION"):
        CartridgePipeline(None).prepare(
            values, ImageSettings(image_background="transparent"), runs_dir=tmp_path
        )
    assert not list(tmp_path.iterdir())
