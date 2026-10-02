"""Sol writing stays separate from the world system; no paid calls in these tests."""

import copy
import json

import pytest
from pydantic import Field, ValidationError

from save_reel.models import ConceptRequest
from save_reel.narration_models import NarrationScript
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import review_text, visual_values
from save_reel.story_models import StorySettings as CurrentStorySettings
from save_reel.story_narration_models import TraveloguePolicy
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prose_models import ProseCandidates, WorldDescription


class StorySettings(CurrentStorySettings):
    """Exercise cached v2 behavior independently of the new default."""

    travelogue: TraveloguePolicy = Field(default_factory=lambda: TraveloguePolicy(
        prompt_version="v2", target_min_words=55, target_max_words=80))


@pytest.fixture(autouse=True)
def prose_v2_defaults(monkeypatch):
    monkeypatch.setattr("save_reel.story_pipeline.StorySettings", StorySettings)


class Spy(MockStoryProvider):
    def __init__(self):
        super().__init__()
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super().generate(**kwargs)


@pytest.fixture
def generated(tmp_path):
    provider = Spy()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="sol", random_seed=41)
    return workflow, provider, run, tmp_path / "sol"


def test_default_writer_has_only_description_examples_and_word_range(generated):
    _, provider, run, directory = generated
    assert run.settings.model == "gpt-6-luna"
    assert run.settings.narration_model == "gpt-6.1-sol"
    assert run.settings.travelogue.prompt_version == "v2"
    for slot in run.saves:
        assert isinstance(slot.travelogue.candidates, ProseCandidates)
        assert slot.travelogue.selected_id == "narration_3"
        slot.travelogue.policy.check(slot.final.brief.narration)
    calls = [c for c in provider.calls if c["stage"] == "narration_prose_candidates"]
    assert len(calls) == 4
    for call, slot in zip(calls, run.saves, strict=True):
        assert set(call["context"]) == {"title", "world_description", "word_range"}
        assert call["context"]["word_range"] == [55, 80]
        assert call["context"]["world_description"] == slot.travelogue.description.description
        assert "Deep End is warm all year" in call["prompt"]
        assert "The Sunroom Line never reaches" in call["prompt"]
        assert "approved_facts" not in call["prompt"]
        assert "critical_survival_rule" not in call["prompt"]
        assert "distinct_fact_count" not in call["prompt"]
    manifest = RunStore(directory).load_manifest()
    versions = {t.name: t.version for t in manifest.story_generation.templates}
    assert versions["story_world_simulation"] == "v3"
    assert versions["story_narration_prose_candidates"] == "v2"
    assert '"naturalness"' in review_text(run)


def test_paragraph_contract_has_no_survival_metadata(generated):
    state = generated[2].saves[0].travelogue
    raw = state.candidates.model_dump()
    assert set(raw["candidates"][0]) == {"candidate_id", "paragraph"}
    for text in ("You live here.", "You " + "live " * 85,
                 state.selected.paragraph.replace(". ", ".\n", 1)):
        invalid = copy.deepcopy(raw)
        invalid["candidates"][0]["paragraph"] = text
        with pytest.raises(ValueError):
            ProseCandidates.model_validate(invalid).check(None, state.policy)
    with pytest.raises(ValidationError):
        ProseCandidates.model_validate({"candidates": raw["candidates"][:2]})
    raw["candidates"][0]["paragraph"] = raw["candidates"][1]["paragraph"]
    with pytest.raises(ValidationError):
        ProseCandidates.model_validate(raw)


def test_selection_prefers_naturalness_and_rejects_invention(generated):
    state = generated[2].saves[0].travelogue
    review = state.review.model_copy(deep=True)
    review.ratings[0].naturalness = 1.0
    assert review.choose(state.candidates) == "narration_1"
    review.ratings[0].grounded = False
    assert review.choose(state.candidates) == "narration_3"
    review.ratings[2].issues = ("A list of instructions rather than flowing prose.",)
    assert review.choose(state.candidates) == "narration_2"
    review.ratings[1].grounded = False
    assert review.choose(state.candidates) is None
    review.ratings[1].candidate_id = "narration_1"
    with pytest.raises(ValueError, match="every candidate"):
        review.check(state.candidates)


def test_cached_resume_and_one_save_regeneration_preserve_worlds(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    assert workflow.resume(directory) == old
    assert not provider.calls
    new = workflow.regenerate(directory, scope="narration", save_id="save_02", run_id="revised")
    assert [c["stage"] for c in provider.calls] == [
        "narration_description", "narration_prose_candidates", "narration_prose_selection"
    ]
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]
    assert new.saves[1].world == old.saves[1].world
    assert visual_values(new.saves[1]) == visual_values(old.saves[1])
    assert new.saves[1].assessments == old.saves[1].assessments


def test_world_requests_novelty_and_visuals_are_unchanged(tmp_path):
    runs, providers = [], []
    for name, policy in (("old", TraveloguePolicy()), ("sol", TraveloguePolicy.sol())):
        p = Spy()
        runs.append(StoryWorkflow(p).create(
            ConceptRequest(), runs_dir=tmp_path / name, run_id="test", random_seed=41,
            settings=StorySettings(travelogue=policy)))
        providers.append(p)
    for old, new in zip(runs[0].saves, runs[1].saves, strict=True):
        assert old.selected == new.selected and old.world == new.world
        assert old.assessments == new.assessments and old.world_review == new.world_review
        assert visual_values(old) == visual_values(new)
    calls = [[c for c in p.calls if not c["stage"].startswith("narration")] for p in providers]
    assert calls[0] == calls[1]
    upgraded = StoryWorkflow(providers[0]).regenerate(
        tmp_path / "old/test", scope="narration", save_id="save_02", narration_style="sol",
        run_id="upgrade")
    assert upgraded.saves[1].travelogue.policy.prompt_version == "v3"
    assert upgraded.saves[0] == runs[0].saves[0]


def test_rejected_narrations_retry_without_resimulating_or_passing_full_rubric(tmp_path):
    class RejectOnce(Spy):
        rejected = False

        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "narration_prose_candidates" and self.rejected:
                for candidate in value.candidates:
                    candidate.paragraph = "Here, " + candidate.paragraph
            if kwargs["stage"] == "narration_prose_selection" and not self.rejected:
                self.rejected = True
                for r in value.ratings:
                    r.issues = ("Too much of a checklist.",)
            return value

    p = RejectOnce()
    run = StoryWorkflow(p).create(ConceptRequest(), runs_dir=tmp_path, run_id="retry")
    assert len(run.saves[0].travelogue.attempts) == 2
    assert len([c for c in p.calls if c["stage"] == "world_simulation"]) == 4
    assert len([c for c in p.calls if c["stage"] == "narration_description"]) == 4
    retry = [c for c in p.calls if c["stage"] == "narration_prose_candidates"][1]
    assert retry["context"]["revision_notes"]
    assert "previous_attempt" not in retry["context"]


@pytest.mark.parametrize("stage,attribute", [
    ("narration_description", "description"),
    ("narration_prose_candidates", "candidates"),
    ("narration_prose_selection", "review"),
])
def test_sdk_uses_sol_medium_with_simple_strict_schemas(generated, stage, attribute):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    value = getattr(generated[2].saves[0].travelogue, attribute)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 0,
            "status": "completed", "model": "gpt-6.1-sol",
            "output": [{"id": "msg_offline", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": value.model_dump_json(), "annotations": []}]}],
        })

    with openai.OpenAI(api_key="offline-test", max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        result = OpenAIStoryProvider(StorySettings(), client=client).generate(
            stage=stage, prompt="Offline test", context={}, response_type=type(value))
    assert result == value
    assert bodies[0]["model"] == "gpt-6.1-sol"
    assert bodies[0]["reasoning"] == {"effort": "medium"}
    assert bodies[0]["text"]["format"]["strict"] is True
    settings = StorySettings(narration_model="configured-writer")
    assert settings.model_for(stage) == "configured-writer"
    assert settings.model_for("world_simulation") == "gpt-6-luna"


def test_handoff_is_compact_and_longer_paragraphs_fit_speech_contract():
    with pytest.raises(ValidationError, match="180 words"):
        WorldDescription(description="word " * 181)
    text = "You " + "occasionally " * 70
    script = NarrationScript(intro="Choose a save.", games=tuple(
        {"title": f"SAVE {i}", "text": text} for i in range(4)))
    assert len(script.games[0].text) > 500
