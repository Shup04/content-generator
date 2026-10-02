import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from save_reel.cli import main
from save_reel.media_models import BrollValues, CartridgeValues, MotionValues
from save_reel.models import ConceptRequest, StageStatus
from save_reel.prompting import (
    BrollStillPromptCompiler,
    BrollVideoPromptCompiler,
    CartridgePromptCompiler,
)
from save_reel.providers.mock_story_v1 import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import review_text
from save_reel.story_history import StoryHistory, novelty_penalty
from save_reel.story_models import (
    ROLES,
    CandidateSet,
    FinalStory,
    NarrationPolicy,
    StoryRun,
    SurvivalDetails,
    normalized,
    word_count,
)
from save_reel.story_models import (
    StorySettings as CurrentStorySettings,
)
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prompting import load_prompts, render_request
from save_reel.story_seeds import make_seeds


class StorySettings(CurrentStorySettings):
    """Pin the original checkpoint/CLI regression tests to their v1 contracts."""

    prompt_version: str = "v1"
    seed_catalog_version: str = "v1"


@pytest.fixture(autouse=True)
def legacy_defaults(monkeypatch):
    monkeypatch.setattr("save_reel.story_pipeline.StorySettings", StorySettings)
    monkeypatch.setattr("save_reel.story_cli.StorySettings", StorySettings)


class SpyProvider(MockStoryProvider):
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super().generate(**kwargs)


@pytest.fixture
def generated(tmp_path):
    provider = SpyProvider()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="episode", random_seed=42)
    return workflow, provider, run, tmp_path / run.run_id


def test_four_valid_final_saves_and_distinct_roles(generated):
    _, provider, run, directory = generated
    assert len(run.saves) == 4
    assert tuple(s.seed.role for s in run.saves) == ROLES
    for field in ("title", "danger_type", "long_term_cost_type"):
        assert len({normalized(getattr(s.selected, field)) for s in run.saves}) == 4
    assert len({s.seed.setting_family for s in run.saves}) == 4
    assert any(s.seed.social_state != "alone" for s in run.saves)
    assert all(len(s.candidates.candidates) == 3 for s in run.saves)
    assert all(s.selected in s.candidates.candidates for s in run.saves)
    assert len(provider.calls) == 12
    assert run.status == StageStatus.COMPLETED
    assert StoryRun.model_validate_json((directory / "story.json").read_text()) == run


@pytest.mark.parametrize("count", [0, 3, 5])
def test_story_requires_exactly_four(generated, count):
    data = generated[2].model_dump()
    data["saves"] = [data["saves"][0]] * count
    with pytest.raises(ValidationError):
        StoryRun.model_validate(data)


@pytest.mark.parametrize("field", ["role", "danger_type", "long_term_cost_type", "setting_family"])
def test_story_rejects_duplicate_categories(generated, field):
    data = generated[2].model_dump()
    data["saves"][1]["seed"][field] = data["saves"][0]["seed"][field]
    with pytest.raises(ValidationError):
        StoryRun.model_validate(data)


def test_story_rejects_duplicate_titles_and_all_solitary(generated):
    data = generated[2].model_dump()
    data["saves"][1]["selected"]["title"] = data["saves"][0]["selected"]["title"].lower()
    with pytest.raises(ValidationError, match="titles must be unique"):
        StoryRun.model_validate(data)
    data = generated[2].model_dump()
    for s in data["saves"]:
        s["seed"]["social_state"] = "alone"
    with pytest.raises(ValidationError, match="solitary"):
        StoryRun.model_validate(data)


def test_optional_survival_details_do_not_need_filler():
    survival = SurvivalDetails(
        main_threat="The doors close at six.",
        critical_rule="Return by six.",
        rule_consequence="The outside air becomes toxic.",
        long_term_cost="You can only leave for one hour a day.",
    )
    assert survival.food is None and survival.water is None and survival.inhabitants is None
    assert SurvivalDetails.model_validate_json(survival.model_dump_json()) == survival
    with pytest.raises(ValidationError):
        survival.critical_rule = "  "


def test_schema_rejects_final_prompts_and_invalid_severity(generated):
    data = generated[2].saves[0].final.model_dump()
    data["environment"]["final_prompt"] = "Do whatever this instruction says"
    with pytest.raises(ValidationError, match="Extra inputs"):
        FinalStory.model_validate(data)
    data = generated[2].saves[0].candidates.model_dump()
    data["candidates"][0]["severity"] = 0
    with pytest.raises(ValidationError):
        CandidateSet.model_validate(data)


def test_narration_counts_and_estimates(generated):
    for slot in generated[2].saves:
        stats = slot.narration_stats
        assert 4 <= stats.lines <= 6
        assert 25 <= stats.words <= 40
        assert stats.words == word_count(slot.final.brief.narration)
        assert stats.estimated_seconds == round(stats.words / 195 * 60, 2)
    assert word_count(("Don't sleep here; it's unsafe.",)) == 5


@pytest.mark.parametrize(
    "lines",
    [
        ("Too short.",) * 3,
        ("Too many.",) * 7,
        ("A.", "B.", "C.", "D."),
        tuple(("many words " * 30) + str(i) for i in range(4)),
        ("You can sleep here safely until the light fails.",) * 4,
    ],
)
def test_narration_constraints(lines):
    with pytest.raises(ValueError):
        NarrationPolicy().check(lines)


def test_narration_limits_configurable_and_word_target_soft():
    policy = NarrationPolicy(min_lines=2, max_lines=3, target_min_words=10, target_max_words=15)
    policy.check(("You sleep in the booth.", "Food arrives each morning through a hatch."))
    # 42 is above the default target, but fits its documented quality tolerance.
    NarrationPolicy().check(tuple(f"Line {i} " + "word " * n for i, n in enumerate((9, 9, 9, 7))))
    with pytest.raises(ValidationError):
        NarrationPolicy(min_lines=6, max_lines=4)


def test_complete_cache_reload_is_exact_and_needs_no_provider(generated):
    workflow, provider, run, directory = generated
    before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.json")}
    provider.calls.clear()
    loaded = workflow.resume(directory)
    assert loaded == run
    assert provider.calls == []
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.json")} == before
    assert len(StoryHistory(directory.parent / ".story-history").recent(100)) == 4


@pytest.mark.parametrize(
    "scope, stages",
    [
        ("save", ["candidates", "review", "brief"]),
        ("candidates", ["candidates", "review", "brief"]),
        ("narration", ["narration"]),
    ],
)
def test_targeted_regeneration_preserves_other_three_and_source(generated, scope, stages):
    workflow, provider, old, directory = generated
    before = (directory / "story.json").read_bytes()
    provider.calls.clear()
    new = workflow.regenerate(
        directory, scope=scope, save_id="save_02", run_id=f"fork-{scope}", random_seed=2026
    )
    assert [c["stage"] for c in provider.calls] == stages
    assert all(c["context"]["save_id"] == "save_02" for c in provider.calls)
    for index in (0, 2, 3):
        assert new.saves[index].model_dump_json() == old.saves[index].model_dump_json()
    if scope == "candidates":
        assert new.saves[1].seed == old.saves[1].seed
    if scope == "save":
        assert new.saves[1].seed != old.saves[1].seed
    if scope == "narration":
        assert new.saves[1].final.environment == old.saves[1].final.environment
        assert new.saves[1].final.cartridge == old.saves[1].final.cartridge
        assert new.saves[1].final.brief.survival == old.saves[1].final.brief.survival
        assert new.saves[1].final.brief.narration != old.saves[1].final.brief.narration
        assert len(StoryHistory(directory.parent / ".story-history").recent(100)) == 4
    assert new.source_run_id == old.run_id
    assert (directory / "story.json").read_bytes() == before
    assert RunStore(directory.parent / new.run_id).load_manifest().story_generation is not None


def test_regenerate_reel_replaces_all(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    new = workflow.regenerate(directory, scope="reel", run_id="all-new", random_seed=23)
    assert len(provider.calls) == 12
    assert all(a.seed != b.seed for a, b in zip(old.saves, new.saves))


def test_manifest_tracks_prompt_versions_and_downstream_templates(generated):
    _, _, run, directory = generated
    reel = RunStore(directory).load_manifest()
    provenance = reel.story_generation
    assert provenance.provider == "mock"
    assert provenance.model == "mock-survival-v1"
    assert {r.name for r in provenance.templates} == {
        "story_rules",
        "story_candidates",
        "story_review",
        "story_brief",
        "story_narration",
        "world_seeds",
    }
    assert all(r.version == "v1" for r in provenance.templates)
    assert (
        provenance.state.sha256
        == hashlib.sha256((directory / "story.json").read_bytes()).hexdigest()
    )
    for save, slot in zip(reel.saves, run.saves):
        root = directory / "saves" / save.save_id
        cart = CartridgeValues.model_validate_json((root / "cartridge_values.json").read_text())
        broll = BrollValues.model_validate_json((root / "broll_values.json").read_text())
        motion = MotionValues.model_validate_json((root / "motion_values.json").read_text())
        cp = CartridgePromptCompiler("v2").render(cart.model_dump(by_alias=True, exclude_none=True))
        bp = BrollStillPromptCompiler("v2").render(broll.model_dump(by_alias=True))
        vp = BrollVideoPromptCompiler("v2").render(
            {**broll.model_dump(by_alias=True), **motion.model_dump(by_alias=True)}
        )
        assert (root / "cartridge_prompt.txt").read_text() == cp.text
        assert (root / "broll_still_prompt.txt").read_text() == bp.text
        assert (root / "broll_video_prompt.txt").read_text() == vp.text
        assert slot.final.cartridge.hint_scene_description in cp.text
        assert slot.final.brief.survival.long_term_cost not in cp.text
        assert slot.final.environment.environment_description in bp.text
        assert save.environment_prompt and save.label_prompt
        for artifact in save.artifacts.values():
            assert (
                hashlib.sha256((directory / artifact.path).read_bytes()).hexdigest()
                == artifact.sha256
            )


def test_history_passed_to_generator_bounded_and_influences_seed_choice(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    run = workflow.create(
        ConceptRequest(),
        runs_dir=directory.parent,
        run_id="next",
        random_seed=42,
        settings=StorySettings(recent_history_count=3),
    )
    assert len(run.history) == 3
    assert all(len(c["context"]["recent_history"]) == 3 for c in provider.calls)
    for call in provider.calls:
        assert all(h.title in call["prompt"] for h in run.history)
    assert all(
        s.seed.specific_setting not in {h.specific_setting for h in run.history} for s in run.saves
    )
    slot = old.saves[0]
    assert (
        novelty_penalty(
            slot.selected,
            slot.seed,
            tuple(StoryHistory(directory.parent / ".story-history").recent(100)),
        )
        > 20
    )


def test_seed_sampling_reproducible_and_settings_diverse():
    settings = StorySettings()
    first, reference = make_seeds(19, settings, ())
    second, again = make_seeds(19, settings, ())
    assert (first, reference) == (second, again)
    all_settings = set()
    for number in range(20):
        seeds, _ = make_seeds(number, settings, ())
        assert len({s.danger_type for s in seeds}) == 4
        assert len({s.long_term_cost_type for s in seeds}) == 4
        all_settings.update(s.specific_setting for s in seeds)
    assert len(all_settings) >= 25


def test_invalid_output_retries_logs_and_passes_feedback(tmp_path, caplog):
    class InvalidOnce(SpyProvider):
        def generate(self, **kwargs):
            if not self.calls:
                self.calls.append(copy.deepcopy(kwargs))
                return {"candidates": []}
            return super().generate(**kwargs)

    provider = InvalidOnce()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="retry")
    assert run.status == StageStatus.COMPLETED
    assert len(provider.calls) == 13
    assert "validation attempt 1 failed" in caplog.text
    assert "candidates:" in provider.calls[1]["prompt"]
    assert (
        json.loads(
            (tmp_path / "retry/story_requests/save_01/candidates/attempt_001.json").read_text()
        )["status"]
        == "invalid"
    )


def test_invalid_output_exhaustion_checkpoints_failure(tmp_path):
    class Invalid(SpyProvider):
        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {"candidates": []}

    provider = Invalid()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="validation exhausted"):
        workflow.create(
            ConceptRequest(),
            runs_dir=tmp_path,
            run_id="bad",
            settings=StorySettings(validation_retries=1),
        )
    assert len(provider.calls) == 2
    saved = workflow.load(tmp_path / "bad")
    assert saved.status == StageStatus.FAILED
    assert saved.saves[0].candidates is None
    assert not (tmp_path / "bad/.story.lock").exists()


def test_resume_reuses_completed_stages_after_failure(tmp_path):
    class Interrupted(SpyProvider):
        failed = False

        def generate(self, **kwargs):
            if (
                kwargs["stage"] == "brief"
                and kwargs["context"]["save_id"] == "save_02"
                and not self.failed
            ):
                self.failed = True
                raise RuntimeError("simulated interruption")
            return super().generate(**kwargs)

    provider = Interrupted()
    workflow = StoryWorkflow(provider)
    with pytest.raises(RuntimeError, match="interruption"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="partial", random_seed=1)
    before = workflow.load(tmp_path / "partial")
    provider.calls.clear()
    run = workflow.resume(tmp_path / "partial")
    assert len(provider.calls) == 3
    assert {c["stage"] for c in provider.calls} == {"brief"}
    assert run.saves[0] == before.saves[0]


def test_cached_response_recovers_checkpoint_write_gap(tmp_path):
    class Interrupted(SpyProvider):
        def generate(self, **kwargs):
            if kwargs["context"]["save_id"] == "save_02":
                raise RuntimeError("stop before next save")
            return super().generate(**kwargs)

    workflow = StoryWorkflow(Interrupted())
    with pytest.raises(RuntimeError):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="gap", random_seed=4)
    run = workflow.load(tmp_path / "gap")
    run.saves[0].candidates = None
    workflow._save(run, RunStore(tmp_path / "gap"))
    provider = SpyProvider()
    result = StoryWorkflow(provider).resume(tmp_path / "gap")
    assert result.status == StageStatus.COMPLETED
    assert not any(
        c["stage"] == "candidates" and c["context"]["save_id"] == "save_01" for c in provider.calls
    )


def test_story_only_cli_never_imports_media_providers(tmp_path):
    # Run in an isolated interpreter, forbidding paid adapters and media assembly imports.
    import subprocess
    import sys

    (tmp_path / "legacy-settings.json").write_text(StorySettings().model_dump_json())
    script = """
import sys
class Forbid:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            'save_reel.providers.openai_image', 'save_reel.providers.minimax_video',
            'save_reel.providers.elevenlabs_speech', 'save_reel.rendering', 'save_reel.narration',
            'save_reel.cartridge', 'save_reel.broll', 'openai', 'httpx',
        }:
            raise AssertionError('story mode imported ' + fullname)
sys.meta_path.insert(0, Forbid())
from save_reel.cli import main
raise SystemExit(main(['generate-concepts', '--count', '2', '--run-id', 'offline',
                      '--seed', '12', '--quiet', '--settings', 'legacy-settings.json']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert len(list(tmp_path.glob("runs/offline-*/manifest.json"))) == 2
    assert len(list(tmp_path.glob("runs/offline-*/story_review.txt"))) == 2


def test_cli_review_and_regeneration(generated, capsys):
    _, _, _, directory = generated
    assert main(["review-concepts", str(directory)]) == 0
    output = capsys.readouterr().out
    assert "Critical rule:" in output and "SAVE 04" in output
    assert (
        main(
            [
                "regenerate-concepts",
                str(directory),
                "--scope",
                "narration",
                "--save-id",
                "save_03",
                "--run-id",
                "cli-fork",
                "--quiet",
            ]
        )
        == 0
    )
    assert (directory.parent / "cli-fork/manifest.json").exists()
    assert (
        main(["generate-concepts", "--count", "0", "--runs-dir", str(directory / "invalid")]) == 1
    )
    assert not (directory / "invalid").exists()


def test_prompt_snapshots_and_untrusted_placeholder_values(generated):
    run = generated[2]
    context = {"theme": "value containing [FEEDBACK] and [CONTEXT] and $foo"}
    text = render_request(run.templates["candidates"], context, "retry detail")
    assert json.dumps(context["theme"]) in text
    assert text.endswith("retry detail\n")
    assert "practical spoken briefing" in text
    assert "Do not reuse recent titles" in text
    assert load_prompts("v1")["brief"].template.version == "v1"


def test_openai_adapter_uses_structured_schema_and_configurable_model():
    pytest.importorskip("openai")
    from save_reel.providers.openai_story import OpenAIStoryProvider
    from save_reel.story_models import NarrationDraft

    calls = []
    expected = NarrationDraft(narration=("A supplied response.",))

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_parsed=expected)

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    settings = StorySettings(model="configured-model")
    provider = OpenAIStoryProvider(settings, client=client)
    result = provider.generate(
        stage="narration", prompt="Prompt snapshot", context={}, response_type=NarrationDraft
    )
    assert result == expected
    assert calls[0]["model"] == "configured-model"
    assert calls[0]["text_format"] is NarrationDraft
    assert calls[0]["store"] is False
    assert StorySettings().model == "gpt-6-luna"


def test_review_missing_optional_fields_are_omitted(generated):
    text = review_text(generated[2])
    assert "Food:" in text
    assert "Water:" not in text
    assert "None" not in text


def test_selection_obeys_ratings_and_rejections(generated):
    data = generated[2].model_dump()
    data["status"] = "running"
    for slot in data["saves"]:
        slot.update(selected=None, final=None, narration_stats=None, selection_score=None)
        for index, rating in enumerate(slot["review"]["ratings"]):
            for metric in (
                "concrete_daily_life",
                "memorable_rule",
                "originality",
                "visual_potential",
                "reason_to_choose",
            ):
                rating[metric] = (5, 4, 0)[index]
            rating["reject"] = index == 0  # Highest numerical score is ineligible.
    run = StoryRun.model_validate(data)
    StoryWorkflow._select(run)
    assert all(s.selected.candidate_id.endswith("_2") for s in run.saves)


def test_selection_resolves_duplicate_titles_across_candidate_sets(generated):
    data = generated[2].model_dump()
    data["status"] = "running"
    for slot in data["saves"]:
        slot.update(selected=None, final=None, narration_stats=None, selection_score=None)
        slot["candidates"]["candidates"][0]["title"] = "SHARED TITLE"
        for index, rating in enumerate(slot["review"]["ratings"]):
            for metric in (
                "concrete_daily_life",
                "memorable_rule",
                "originality",
                "visual_potential",
                "reason_to_choose",
            ):
                rating[metric] = 5 if index == 0 else 1
    run = StoryRun.model_validate(data)
    StoryWorkflow._select(run)
    assert len({s.selected.title for s in run.saves}) == 4
    assert sum(s.selected.title == "SHARED TITLE" for s in run.saves) == 1


def test_completed_cache_tampering_is_reported(generated):
    workflow, provider, _, directory = generated
    path = directory / "story.json"
    data = json.loads(path.read_text())
    data["request"]["theme"] = "a changed theme"
    path.write_text(json.dumps(data))
    provider.calls.clear()
    with pytest.raises(ValueError, match="checksum"):
        workflow.resume(directory)
    assert provider.calls == []


def test_resume_repairs_interrupted_exports_without_model_calls(generated):
    workflow, provider, _, directory = generated
    manifest = RunStore(directory).load_manifest()
    manifest.story_generation = None
    RunStore(directory).save_manifest(manifest)
    provider.calls.clear()
    workflow.resume(directory)
    assert provider.calls == []
    assert RunStore(directory).load_manifest().story_generation is not None


def test_different_model_cannot_resume_partial_generation(tmp_path):
    class Interrupted(SpyProvider):
        def generate(self, **kwargs):
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        StoryWorkflow(Interrupted()).create(ConceptRequest(), runs_dir=tmp_path, run_id="partial")
    different = SpyProvider()
    different.model = "different-model"
    with pytest.raises(ValueError, match="saved provider and model"):
        StoryWorkflow(different).resume(tmp_path / "partial")
    assert different.calls == []


@pytest.mark.parametrize(
    "field", ["main_threat", "critical_rule", "rule_consequence", "long_term_cost"]
)
def test_final_brief_cannot_change_selected_facts(generated, field):
    _, _, run, _ = generated
    result = run.saves[0].final.model_copy(deep=True)
    setattr(result.brief.survival, field, "A different story fact.")
    with pytest.raises(ValueError, match="preserve the selected text"):
        StoryWorkflow._check_final(run, run.saves[0], result)


def test_provider_validation_feedback_preserves_snapshot_on_resume(tmp_path, monkeypatch):
    class Interrupted(SpyProvider):
        def generate(self, **kwargs):
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        StoryWorkflow(Interrupted()).create(ConceptRequest(), runs_dir=tmp_path, run_id="snapshot")
    before = StoryWorkflow.load(tmp_path / "snapshot")

    def forbidden(*args, **kwargs):
        raise AssertionError("resume must not load current story prompts")

    monkeypatch.setattr("save_reel.story_pipeline.load_prompts", forbidden)
    provider = SpyProvider()
    after = StoryWorkflow(provider).resume(tmp_path / "snapshot")
    assert after.templates == before.templates


@pytest.mark.parametrize("schema_name", ["candidates", "review", "final"])
def test_openai_sdk_schema_and_response_parsing_over_mock_http(generated, schema_name):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    value = getattr(generated[2].saves[0], schema_name)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        return httpx.Response(
            200,
            json={
                "id": "resp_offline",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "id": "msg_offline",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": value.model_dump_json(),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    with openai.OpenAI(
        api_key="offline-test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        provider = OpenAIStoryProvider(StorySettings(), client=client)
        result = provider.generate(
            stage=schema_name, prompt="Offline prompt", context={}, response_type=type(value)
        )
    assert result == value
    assert len(requests) == 1


def test_cli_model_precedence_loads_dotenv_without_network(tmp_path, monkeypatch):
    pytest.importorskip("dotenv")
    from save_reel import story_cli

    # A fake adapter keeps both calls offline while exercising CLI settings resolution.
    seen = []

    def provider(name, settings, env_file):
        seen.append(settings.model)
        return MockStoryProvider()

    monkeypatch.setattr(story_cli, "_provider", provider)
    monkeypatch.delenv("SAVE_REEL_STORY_MODEL", raising=False)
    # Restore the environment key even though dotenv assigns it internally.
    monkeypatch.setenv("SAVE_REEL_STORY_MODEL", "")
    monkeypatch.delenv("SAVE_REEL_STORY_MODEL")
    env_file = tmp_path / "local.env"
    env_file.write_text("SAVE_REEL_STORY_MODEL=env-model\n")
    args = [
        "generate-concepts",
        "--provider",
        "openai",
        "--env-file",
        str(env_file),
        "--runs-dir",
        str(tmp_path),
        "--quiet",
    ]
    assert main([*args, "--run-id", "env"]) == 0
    assert main([*args, "--run-id", "override", "--model", "cli-model"]) == 0
    assert seen == ["env-model", "cli-model"]


def test_custom_line_policy_works_in_mock_workflow(tmp_path):
    policy = NarrationPolicy(min_lines=5, max_lines=6, target_min_words=30, target_max_words=50)
    run = StoryWorkflow(MockStoryProvider()).create(
        ConceptRequest(),
        settings=StorySettings(narration=policy),
        runs_dir=tmp_path,
        run_id="longer",
        random_seed=11,
    )
    assert all(5 <= s.narration_stats.lines <= 6 for s in run.saves)


def test_corrupt_history_is_reported_instead_of_silently_ignored(tmp_path):
    directory = tmp_path / "history"
    directory.mkdir()
    (directory / "bad.json").write_text('{"title": "incomplete"}')
    with pytest.raises(ValidationError):
        StoryHistory(directory).recent(10)
    assert StoryHistory(directory).recent(0) == ()


def test_critic_rejecting_all_candidates_does_not_get_retried_into_accepting_them(tmp_path):
    class RejectingCritic(SpyProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"] == "review" and kwargs["context"]["save_id"] == "save_01":
                for rating in result.ratings:
                    rating.reject = True
            return result

    provider = RejectingCritic()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="no eligible candidates"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="rejected")
    assert len(provider.calls) == 8
    assert all(c["stage"] != "brief" for c in provider.calls)
    saved = workflow.load(tmp_path / "rejected")
    assert saved.saves[0].review is not None
    assert saved.saves[0].selected is None
    provider.calls.clear()
    with pytest.raises(ValueError, match="no eligible candidates"):
        workflow.resume(tmp_path / "rejected")
    assert provider.calls == []
