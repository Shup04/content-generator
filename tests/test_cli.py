import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from save_reel.cli import main
from save_reel.storage import RunStore


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_cli_works_outside_repository(tmp_path, entrypoint):
    command = [sys.executable, "-m", "save_reel"]
    if entrypoint == "console":
        executable = shutil.which("save-reel", path=str(Path(sys.executable).parent))
        assert executable is not None, "install the package before running tests"
        command = [executable]
    result = subprocess.run(
        [*command, "create", "--run-id", "cli-demo", "--theme", "quiet adventures"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runs/cli-demo/manifest.json"
    assert "Run completed" in result.stderr
    reel = RunStore(tmp_path / "runs" / "cli-demo").load_manifest()
    assert len(reel.saves) == 4
    assert reel.request.theme == "quiet adventures"
    assert all(save.label_prompt and save.broll_still_prompt for save in reel.saves)


def test_cli_reports_collisions(tmp_path, caplog):
    args = ["create", "--runs-dir", str(tmp_path), "--run-id", "same"]
    assert main(args) == 0
    assert main(args) == 1
    assert "Create failed" in caplog.text


def test_missing_template_does_not_create_run(tmp_path, caplog):
    assert main(["create", "--runs-dir", str(tmp_path), "--template-version", "v999"]) == 1
    assert not list(tmp_path.iterdir())
    assert "cannot read label/v999.txt" in caplog.text


def test_cli_can_select_template_versions_independently(tmp_path):
    prompts = tmp_path / "prompts"
    (prompts / "label").mkdir(parents=True)
    (prompts / "broll_still").mkdir()
    (prompts / "label" / "v2.txt").write_text("Label: [GAME_TITLE]")
    (prompts / "broll_still" / "v3.txt").write_text("B-roll: [WORLD_SCENE]")
    runs = tmp_path / "runs"
    assert (
        main(
            [
                "create",
                "--runs-dir",
                str(runs),
                "--run-id",
                "versions",
                "--prompts-dir",
                str(prompts),
                "--label-template-version",
                "v2",
                "--broll-template-version",
                "v3",
            ]
        )
        == 0
    )
    reel = RunStore(runs / "versions").load_manifest()
    assert all(save.label_prompt.template.version == "v2" for save in reel.saves)
    assert all(save.broll_still_prompt.template.version == "v3" for save in reel.saves)


def test_unknown_provider_is_rejected():
    with pytest.raises(SystemExit) as exc:
        main(["create", "--provider", "openai"])
    assert exc.value.code == 2
