"""World-first narration contracts. All model calls are offline fakes."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import review_text, visual_values
from save_reel.story_history import StoryHistory
from save_reel.story_models import (
    GroundedNarration,
    GroundingReview,
    StoryRun,
    WorldApproval,
    WorldSimulation,
    WorldSpec,
)
from save_reel.story_models import StorySettings as CurrentStorySettings
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_simulation import check_world


class StorySettings(CurrentStorySettings):
    """Keep these regression tests on the saved v3 briefing contract."""

    travelogue: None = None


@pytest.fixture(autouse=True)
def briefing_defaults(monkeypatch):
    monkeypatch.setattr("save_reel.story_pipeline.StorySettings", StorySettings)
    monkeypatch.setattr("save_reel.story_cli.StorySettings", StorySettings)


class SpyProvider(MockStoryProvider):
    def __init__(self):
        super().__init__("v3")
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super().generate(**kwargs)


@pytest.fixture
def generated(tmp_path):
    provider = SpyProvider()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="v3", random_seed=41)
    return workflow, provider, run, tmp_path / "v3"


def test_world_approval_precedes_fact_only_narration(generated):
    _, provider, run, directory = generated
    assert run.schema_version == "3.0"
    assert len(run.saves) == 4
    assert "brief" not in [c["stage"] for c in provider.calls]
    for slot in run.saves:
        calls = [c for c in provider.calls if c["context"].get("save_id") == slot.save_id]
        assert [c["stage"] for c in calls] == [
            "candidates", "review", "world_simulation", "world_review",
            "narration", "narration_review",
        ]
        narration = next(c for c in calls if c["stage"] == "narration")
        context = narration["context"]
        assert context["approved_facts"] == slot.world.spec.facts()
        assert not {"selected", "seed", "creative_history", "other_saves", "theme"} & context.keys()
        assert slot.selected.visual_hook not in narration["prompt"]
        assert "palette" not in context["approved_facts"]
        assert "narration" not in slot.world.model_dump()
        assert slot.world_review.approved(run.settings.novelty)
        assert not slot.grounding_review.problems(slot.narration_draft)
        request = json.loads(next((directory / "story_requests" / slot.save_id / "narration")
                                  .glob("*.json")).read_text())
        assert request["reasoning_effort"] == "medium"
        world_request = json.loads(next((directory / "story_requests" / slot.save_id /
                                        "world_simulation").glob("*.json")).read_text())
        assert world_request["reasoning_effort"] == "high"


def test_world_schema_is_fact_only_and_explicit(generated):
    world = generated[2].saves[0].world
    fields = set(WorldSpec.model_fields)
    assert {
        "environment", "food", "water", "shelter", "inhabitants", "threat", "warning_signs",
        "critical_survival_rule", "consequence_if_broken", "escape_conditions", "daily_routine",
        "long_term_cost",
    } <= fields
    raw = world.model_dump()
    raw["spec"]["narration"] = ["Invent a creepy ending."]
    with pytest.raises(ValidationError, match="Extra inputs"):
        WorldSimulation.model_validate(raw)
    raw["spec"].pop("narration")
    raw["spec"].pop("warning_signs")
    with pytest.raises(ValidationError, match="warning_signs"):
        WorldSimulation.model_validate(raw)
    assert world.spec.water is None
    assert "water" not in world.spec.facts()


def test_missing_fact_and_tone_balance_fail_locally(generated):
    slot, policy = generated[2].saves[0], generated[2].settings.narration
    draft = slot.narration_draft.model_copy(deep=True)
    draft.lines[0].fact_refs = ("water",)
    with pytest.raises(ValueError, match="missing"):
        draft.check(slot.world.spec, policy)
    draft = slot.narration_draft.model_copy(deep=True)
    for line in draft.lines:
        line.tone = "unsettling"
    with pytest.raises(ValueError, match="practical majority"):
        draft.check(slot.world.spec, policy)
    raw = slot.narration_draft.model_dump()
    raw["lines"][0]["fact_refs"] = ["imaginary_supply"]
    with pytest.raises(ValidationError):
        GroundedNarration.model_validate(raw)


@pytest.mark.parametrize("field", ["threat", "critical_survival_rule", "consequence_if_broken"])
def test_world_cannot_replace_approved_mechanics(generated, field):
    slot = generated[2].saves[0]
    world = slot.world.model_copy(deep=True)
    setattr(world.spec, field, "An unrelated mechanic.")
    with pytest.raises(ValueError, match="preserve"):
        check_world(slot, world)


def test_unapproved_world_cannot_reach_narration(tmp_path):
    class RejectWorld(SpyProvider):
        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "world_review":
                value.causal_coherence = 0.2
                value.issues = ("The warning cannot detect the threat.",)
            return value

    provider = RejectWorld()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="world simulation approval exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="rejected",
                        settings=StorySettings(validation_retries=1))
    assert not any(c["stage"].startswith("narration") for c in provider.calls)
    assert sum(c["stage"] == "world_simulation" for c in provider.calls) == 2
    saved = workflow.load(tmp_path / "rejected")
    assert len(saved.saves[0].world_attempts) == 2
    assert "warning cannot detect" in (tmp_path / "rejected/story_review.txt").read_text()


@pytest.mark.parametrize("problem", ["invented", "decorative", "repetition", "tone"])
def test_grounding_rejection_rewrites_narration_without_changing_world(tmp_path, problem):
    class RejectLineOnce(SpyProvider):
        rejected = False

        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "narration" and not self.rejected:
                # A valid fact reference alone must not prove an invented claim.
                value.lines[1].text = "You receive unlimited canned food every morning."
            if kwargs["stage"] == "narration_review" and not self.rejected:
                line = value.lines[1]
                if problem == "invented":
                    line.supported_by_facts = False
                elif problem == "decorative":
                    line.changes_practical_understanding = False
                elif problem == "repetition":
                    line.repeats_information = True
                else:
                    line.tone = "unsettling"
                line.issue = problem
                self.rejected = True
            return value

    provider = RejectLineOnce()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="repaired")
    first = run.saves[0]
    assert len(first.world_attempts) == 1
    assert len(first.narration_attempts) == 2
    assert "unlimited canned" not in " ".join(first.final.brief.narration)
    assert problem in first.narration_attempts[0].review.problems(
        first.narration_attempts[0].draft
    )[0]
    assert sum(c["stage"] == "world_simulation" for c in provider.calls) == 4
    retry = [c for c in provider.calls if c["stage"] == "narration"][1]
    assert retry["context"]["previous_attempt"]


def test_persistent_grounding_failure_stops_with_diagnostics(tmp_path):
    class BadReviewer(SpyProvider):
        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "narration_review":
                value.lines[0].changes_practical_understanding = False
                value.lines[0].issue = "Only describes the image."
            return value

    provider = BadReviewer()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="grounding approval exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="bad",
                        settings=StorySettings(validation_retries=1))
    saved = workflow.load(tmp_path / "bad")
    assert len(saved.saves[0].narration_attempts) == 2
    assert saved.saves[0].world_review.approved(saved.settings.novelty)
    assert sum(c["stage"] == "narration" for c in provider.calls) == 2
    assert "Only describes the image" in review_text(saved)


def test_resume_and_narration_only_regeneration_preserve_approved_world(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    assert workflow.resume(directory) == old
    assert not provider.calls
    new = workflow.regenerate(directory, scope="narration", save_id="save_02", run_id="spoken")
    assert [c["stage"] for c in provider.calls] == ["narration", "narration_review"]
    assert new.saves[1].world == old.saves[1].world
    assert new.saves[1].world_review == old.saves[1].world_review
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]
    assert visual_values(new.saves[1]) == visual_values(old.saves[1])
    assert len(StoryHistory(directory.parent / ".story-history").recent(100)) == 4


def test_interrupted_narration_resumes_without_regenerating_worlds(tmp_path):
    class Interrupt(SpyProvider):
        failed = False

        def generate(self, **kwargs):
            if kwargs["stage"] == "narration" and not self.failed:
                self.failed = True
                raise RuntimeError("connection interrupted")
            return super().generate(**kwargs)

    provider = Interrupt()
    workflow = StoryWorkflow(provider)
    with pytest.raises(RuntimeError):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="resume")
    saved = workflow.load(tmp_path / "resume")
    world = saved.saves[0].world.model_copy(deep=True)
    provider.calls.clear()
    completed = workflow.resume(tmp_path / "resume")
    assert completed.saves[0].world == world
    assert not any(c["stage"] == "world_simulation" and c["context"]["save_id"] == "save_01"
                   for c in provider.calls)


def test_completed_cache_requires_approved_facts_and_exact_export(generated):
    raw = generated[2].model_dump()
    raw["saves"][0]["grounding_review"]["lines"][0]["supported_by_facts"] = False
    with pytest.raises(ValidationError, match="grounding"):
        StoryRun.model_validate(raw)
    raw = generated[2].model_dump()
    raw["saves"][0]["final"]["brief"]["survival"]["food"] = "An invented food source."
    with pytest.raises(ValidationError, match="approved world"):
        StoryRun.model_validate(raw)


def test_one_save_regeneration_leaves_the_other_worlds_unchanged(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    new = workflow.regenerate(directory, scope="save", save_id="save_03", run_id="replacement")
    assert new.saves[2].selected.title != old.saves[2].selected.title
    for i in (0, 1, 3):
        assert new.saves[i] == old.saves[i]
    assert all(c["stage"] == "reel_review" or c["context"]["save_id"] == "save_03"
               for c in provider.calls)


def test_grounding_review_must_cover_all_lines_in_order(generated):
    slot = generated[2].saves[0]
    review = slot.grounding_review.model_copy(deep=True)
    review.lines = review.lines[:-1]
    with pytest.raises(ValueError, match="each narration line"):
        review.check(slot.narration_draft)


def test_world_revision_retains_rejection_then_approves_before_narration(tmp_path):
    class Revise(SpyProvider):
        rejected = False

        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "world_simulation" and self.rejected:
                value.spec.water = "Condensate from the air filters is collected in sealed tanks."
            if kwargs["stage"] == "world_review" and not self.rejected:
                value.practical_system = False
                value.issues = ("Resolve drinking water within the station system.",)
                self.rejected = True
            return value

    provider = Revise()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="revised")
    assert len(run.saves[0].world_attempts) == 2
    assert run.saves[0].world.spec.water is not None
    revisions = [c for c in provider.calls if c["stage"] == "world_simulation"
                 and c["context"]["save_id"] == "save_01"]
    assert revisions[0]["context"]["previous_simulation"] is None
    assert revisions[1]["context"]["previous_simulation"] == (
        run.saves[0].world_attempts[0].simulation.model_dump(mode="json")
    )
    assert revisions[1]["context"]["previous_review"] == (
        run.saves[0].world_attempts[0].review.model_dump(mode="json")
    )
    first_narration = next(c for c in provider.calls if c["stage"] == "narration")
    assert first_narration["context"]["approved_facts"]["water"] == run.saves[0].world.spec.water


def test_world_and_grounding_export_with_existing_visual_templates(generated):
    _, _, run, directory = generated
    manifest = RunStore(directory).load_manifest()
    versions = {r.name: r.version for r in manifest.story_generation.templates}
    assert versions["story_world_simulation"] == "v3"
    assert versions["story_narration"] == "v3"
    assert versions["world_seeds"] == "v2"
    for save, slot in zip(manifest.saves, run.saves, strict=True):
        for key in ("world_spec", "world_review", "narration_grounding", "narration_review"):
            assert (directory / save.artifacts[key].path).exists()
        text = (directory / save.artifacts["cartridge_prompt"].path).read_text()
        assert slot.selected.title in text
        assert slot.world.cartridge.hint_scene_description in text
    review = review_text(run)
    assert "WORLD SIMULATION" in review and "NARRATION GROUNDING" in review
    assert "Practical change:" in review


def test_world_revision_retains_earlier_feedback_after_new_objection(tmp_path):
    class ReviseTwice(SpyProvider):
        rejected = 0

        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "world_simulation":
                if self.rejected >= 1:
                    value.spec.water = "Rainwater is collected in sealed tanks."
                if self.rejected >= 2:
                    value.spec.warning_signs = (
                        "A warning bell rings ten minutes before the doors close.",
                    )
            if kwargs["stage"] == "world_review" and self.rejected < 2:
                value.practical_system = False
                value.issues = (("Explain the water supply.", "Explain the warning lead time.")
                                [self.rejected],)
                self.rejected += 1
            return value

    provider = ReviseTwice()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="history")
    calls = [c for c in provider.calls if c["stage"] == "world_simulation"
             and c["context"]["save_id"] == "save_01"]
    assert len(run.saves[0].world_attempts) == 3
    assert calls[2]["context"]["earlier_review_issues"] == ("Explain the water supply.",)
    assert calls[2]["context"]["previous_review"]["issues"] == [
        "Explain the warning lead time."
    ]


@pytest.mark.parametrize("stage, effort", [
    ("candidates", "high"), ("world_simulation", "high"), ("world_review", "high"),
    ("narration", "medium"), ("narration_review", "medium"),
])
def test_openai_uses_stage_reasoning_effort(stage, effort):
    pytest.importorskip("openai")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_parsed="stub")

    provider = OpenAIStoryProvider(
        StorySettings(), client=SimpleNamespace(responses=SimpleNamespace(parse=parse))
    )
    provider.generate(stage=stage, prompt="Test", context={}, response_type=WorldSimulation)
    assert calls[0]["model"] == "gpt-6-luna"
    assert calls[0]["reasoning"] == {"effort": effort}
    assert calls[0]["text_format"] is WorldSimulation
    assert calls[0]["store"] is False


def test_reasoning_configurable_and_legacy_requests_unchanged():
    settings = StorySettings(world_reasoning_effort="medium", narration_reasoning_effort="low")
    assert settings.effort_for("world_simulation") == "medium"
    assert settings.effort_for("narration") == "low"
    for version in ("v1", "v2"):
        assert StorySettings(prompt_version=version, seed_catalog_version=version).effort_for(
            "narration"
        ) is None


def test_candidate_effort_override_preserves_world_and_narration_efforts():
    settings = StorySettings(candidate_reasoning_effort="medium")
    assert settings.effort_for("candidates") == "medium"
    assert settings.effort_for("world_simulation") == "high"
    assert settings.effort_for("world_review") == "high"
    assert settings.effort_for("narration_prose_candidate") == "medium"
    assert StorySettings().effort_for("candidates") == "high"
    assert StorySettings.model_validate_json(settings.model_dump_json()) == settings
    assert StorySettings(prompt_version="v2", seed_catalog_version="v2",
                         candidate_reasoning_effort="medium").effort_for("candidates") is None


def test_new_schemas_convert_to_strict_sdk_format():
    pytest.importorskip("openai")
    from openai.lib._pydantic import to_strict_json_schema

    for cls in (WorldSimulation, WorldApproval, GroundedNarration, GroundingReview):
        schema = to_strict_json_schema(cls)
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(schema["required"])


@pytest.mark.parametrize("stage, field", [
    ("world_simulation", "world"), ("world_review", "world_review"),
    ("narration", "narration_draft"), ("narration_review", "grounding_review"),
])
def test_real_sdk_parses_new_structured_schemas_over_mock_http(generated, stage, field):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    value = getattr(generated[2].saves[0], field)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 0,
            "status": "completed", "model": "gpt-6-luna",
            "output": [{
                "id": "msg_offline", "type": "message", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": value.model_dump_json(), "annotations": [],
                }],
            }],
        })

    with openai.OpenAI(api_key="offline-test-key", max_retries=0,
                       http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        provider = OpenAIStoryProvider(StorySettings(), client=client)
        result = provider.generate(stage=stage, prompt="Offline", context={},
                                   response_type=type(value))
    assert result == value
    assert len(bodies) == 1
    assert bodies[0]["reasoning"]["effort"] == StorySettings().effort_for(stage)
    assert bodies[0]["text"]["format"]["strict"] is True
