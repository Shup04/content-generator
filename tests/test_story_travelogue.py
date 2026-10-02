"""Narration-only travelogues; all provider calls use fakes, never paid APIs."""

import copy
import json

import pytest
from pydantic import ValidationError

from save_reel.cli import main
from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import review_text, visual_values
from save_reel.story_models import StoryRun, StorySettings, word_count
from save_reel.story_narration_models import (
    TravelogueCandidates,
    TraveloguePolicy,
    TravelogueReview,
)
from save_reel.story_pipeline import StoryWorkflow


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
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="travelogue", random_seed=41,
                          settings=StorySettings(travelogue=TraveloguePolicy()))
    return workflow, provider, run, tmp_path / "travelogue"


def test_three_paragraphs_from_same_approved_world_and_medium_selection(generated):
    _, provider, run, directory = generated
    assert len(run.saves) == 4
    for slot in run.saves:
        calls = [c for c in provider.calls if c["context"].get("save_id") == slot.save_id]
        assert [c["stage"] for c in calls][-4:] == [
            "world_simulation", "world_review", "narration_candidates", "narration_selection"
        ]
        assert slot.world_review.approved(run.settings.novelty)
        for call in calls[-2:]:
            context = call["context"]
            assert context["approved_facts"] == slot.world.spec.facts()
            excluded = {"selected", "seed", "creative_history", "other_saves", "theme"}
            assert not excluded & context.keys()
            assert context["narration_policy"]["target_min_words"] == 40
            record = json.loads(next((directory / "story_requests" / slot.save_id /
                                      call["stage"]).glob("*.json")).read_text())
            assert record["reasoning_effort"] == "medium"
        state = slot.travelogue
        assert len(state.candidates.candidates) == 3
        assert state.selected_id == "narration_3"  # fixture's strongest flow, not first result
        for candidate in state.candidates.candidates:
            assert 40 <= word_count((candidate.paragraph,)) <= 60
            assert "\n" not in candidate.paragraph
            assert len(candidate.facts) in (3, 4)
            assert len(candidate.sensory_details) in (1, 2)
        assert slot.final.brief.narration == (state.selected.paragraph,)
        assert slot.narration_stats.lines == 1
        assert slot.narration_draft is None and slot.grounding_review is None


@pytest.mark.parametrize("change, error", [
    (lambda c: c.update(paragraph="You sleep here."), "words"),
    (lambda c: c.update(paragraph=c["paragraph"] + " More words." * 40), "words"),
    (lambda c: c.update(paragraph=c["paragraph"].replace(". ", ".\n", 1)), "paragraph"),
    (lambda c: c["facts"][0].update(source="imagined_fact"), "approved world"),
    (lambda c: c["facts"][0].update(quote="New food arrives at midnight."), "approved world"),
    (lambda c: c.update(ordinary_activity="An activity absent from the paragraph"), "quote"),
])
def test_invalid_text_or_sources_rejected(generated, change, error):
    slot = generated[2].saves[0]
    raw = slot.travelogue.candidates.model_dump()
    change(raw["candidates"][0])
    candidates = TravelogueCandidates.model_validate(raw)
    with pytest.raises(ValueError, match=error):
        candidates.check(slot.world.spec, slot.travelogue.policy)


def test_exactly_three_distinct_candidates_and_three_or_four_facts(generated):
    raw = generated[2].saves[0].travelogue.candidates.model_dump()
    for field, value in (("candidates", raw["candidates"][:2]),):
        with pytest.raises(ValidationError):
            TravelogueCandidates.model_validate({**raw, field: value})
    raw["candidates"][1]["candidate_id"] = raw["candidates"][0]["candidate_id"]
    with pytest.raises(ValidationError, match="distinct"):
        TravelogueCandidates.model_validate(raw)
    raw = generated[2].saves[0].travelogue.candidates.model_dump()
    raw["candidates"][0]["facts"] = raw["candidates"][0]["facts"][:2]
    with pytest.raises(ValidationError):
        TravelogueCandidates.model_validate(raw)


@pytest.mark.parametrize("field,value", [
    ("grounded", False), ("second_person", False), ("connected_prose", False),
    ("checklist_like", True), ("sensory_detail_count", 0), ("sensory_detail_count", 3),
    ("ordinary_activity_present", False), ("distinct_fact_count", 7),
    ("main_danger_count", 2), ("creepy_asides_or_poetry", True), ("forced_twist", True),
])
def test_style_and_invention_failures_cannot_win(generated, field, value):
    state = generated[2].saves[0].travelogue
    review = state.review.model_copy(deep=True)
    setattr(review.ratings[2], field, value)
    assert review.choose(state.candidates) == "narration_2"


def test_no_forced_danger_and_prioritized_selection(generated):
    state = generated[2].saves[0].travelogue
    review = state.review.model_copy(deep=True)
    for rating in review.ratings:
        rating.spoken_flow = 0.9
        rating.not_list_like = 0.9
        rating.cliche_free = 0.9
        rating.main_danger_count = 0
    review.ratings[1].distinct_fact_count = 3
    assert review.choose(state.candidates) == "narration_2"
    review.ratings[0].cliche_free = 1.0
    assert review.choose(state.candidates) == "narration_1"
    review.ratings[2].not_list_like = 1.0
    assert review.choose(state.candidates) == "narration_3"
    review.ratings[1].spoken_flow = 1.0
    assert review.choose(state.candidates) == "narration_2"


def test_review_ids_and_cached_selection_are_validated(generated):
    state = generated[2].saves[0].travelogue
    review = state.review.model_copy(deep=True)
    review.ratings[0].candidate_id = "narration_2"
    with pytest.raises(ValueError, match="every candidate"):
        review.check(state.candidates)
    raw = generated[2].model_dump()
    raw["saves"][0]["travelogue"]["selected_id"] = "narration_1"
    with pytest.raises(ValidationError, match="highest-ranked"):
        StoryRun.model_validate(raw)
    raw = generated[2].model_dump()
    raw["saves"][0]["final"]["brief"]["narration"] = ["Invented narration."]
    with pytest.raises(ValidationError, match="selected travelogue"):
        StoryRun.model_validate(raw)


def test_failed_style_review_regenerates_only_narration(tmp_path):
    class RejectOnce(SpyProvider):
        rejected = False

        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "narration_candidates" and self.rejected:
                for candidate in value.candidates:
                    candidate.paragraph = "Here, " + candidate.paragraph
            if kwargs["stage"] == "narration_selection" and not self.rejected:
                for rating in value.ratings:
                    rating.checklist_like = True
                    rating.issues = ("A sequence of commands, not a travelogue.",)
                self.rejected = True
            return value

    provider = RejectOnce()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="retry",
                                         settings=StorySettings(travelogue=TraveloguePolicy()))
    slot = run.saves[0]
    assert len(slot.travelogue.attempts) == 2
    assert len(slot.world_attempts) == 1
    calls = [c for c in provider.calls if c["stage"] == "narration_candidates"]
    assert calls[1]["context"]["previous_attempt"]["review"]["ratings"][0]["issues"]
    assert calls[0]["context"]["approved_facts"] == calls[1]["context"]["approved_facts"]
    assert "A sequence of commands" in review_text(run)


def test_persistent_rejections_stop_without_modifying_world(tmp_path):
    class Reject(SpyProvider):
        def generate(self, **kwargs):
            value = super().generate(**kwargs)
            if kwargs["stage"] == "narration_selection":
                for rating in value.ratings:
                    rating.grounded = False
                    rating.issues = ("Invented weather.",)
            return value

    provider = Reject()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="travelogue narration approval exhausted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="bad",
                        settings=StorySettings(validation_retries=1, travelogue=TraveloguePolicy()))
    saved = workflow.load(tmp_path / "bad")
    assert len(saved.saves[0].travelogue.attempts) == 2
    assert len(saved.saves[0].world_attempts) == 1
    assert saved.saves[0].final is None
    # An unchanged rejected draft keeps its cached rejection, rather than rerolling a critic.
    assert sum(c["stage"] == "narration_selection" for c in provider.calls) == 1


def test_reload_regenerate_one_narration_and_compile_visuals(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    assert workflow.resume(directory) == old
    assert not provider.calls
    new = workflow.regenerate(directory, scope="narration", save_id="save_02", run_id="spoken")
    assert [c["stage"] for c in provider.calls] == [
        "narration_candidates", "narration_selection"
    ]
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]
    assert new.saves[1].world == old.saves[1].world
    assert new.saves[1].world_review == old.saves[1].world_review
    assert new.saves[1].selected == old.saves[1].selected
    assert new.saves[1].assessments == old.saves[1].assessments
    assert visual_values(new.saves[1]) == visual_values(old.saves[1])
    for name in ("cartridge_prompt.txt", "environment_prompt.txt", "broll_still_prompt.txt"):
        assert (directory / "saves/save_02" / name).read_bytes() == (
            directory.parent / "spoken/saves/save_02" / name
        ).read_bytes()


def test_review_interruption_reuses_three_saved_candidates(tmp_path):
    class Interrupt(SpyProvider):
        interrupted = False

        def generate(self, **kwargs):
            if kwargs["stage"] == "narration_selection" and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("Interrupted review")
            return super().generate(**kwargs)

    provider = Interrupt()
    workflow = StoryWorkflow(provider)
    with pytest.raises(RuntimeError, match="Interrupted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="resume",
                        settings=StorySettings(travelogue=TraveloguePolicy()))
    saved = workflow.load(tmp_path / "resume")
    original = saved.saves[0].travelogue.candidates
    provider.calls.clear()
    complete = workflow.resume(tmp_path / "resume")
    assert complete.saves[0].travelogue.candidates == original
    calls = [c["stage"] for c in provider.calls if c["context"].get("save_id") == "save_01"]
    assert calls == ["narration_selection"]


def test_narration_default_does_not_change_world_generation_or_novelty(tmp_path):
    runs, providers = [], []
    for name, policy in (("briefing", None), ("travelogue", TraveloguePolicy())):
        provider = SpyProvider()
        runs.append(StoryWorkflow(provider).create(
            ConceptRequest(), runs_dir=tmp_path / name, run_id="test", random_seed=41,
            settings=StorySettings(travelogue=policy),
        ))
        providers.append(provider)
    for old, new in zip(*[r.saves for r in runs], strict=True):
        assert old.seed == new.seed and old.selected == new.selected
        assert old.assessments == new.assessments and old.world == new.world
        assert old.world_review == new.world_review and visual_values(old) == visual_values(new)
    calls = [[c for c in p.calls if not c["stage"].startswith("narration")] for p in providers]
    assert calls[0] == calls[1]  # includes actual world/candidate prompts and history context


def test_old_checkpoint_resumes_unchanged_and_explicit_cli_upgrade_is_narration_only(tmp_path):
    workflow = StoryWorkflow(SpyProvider())
    old = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="legacy", random_seed=41,
                          settings=StorySettings(travelogue=None))
    raw = old.model_dump(mode="json")
    raw["settings"].pop("travelogue")
    for slot in raw["saves"]:
        slot.pop("travelogue")
    (tmp_path / "legacy/story.json").write_text(json.dumps(raw))
    assert workflow.load(tmp_path / "legacy").settings.travelogue is None
    assert main(["regenerate-concepts", str(tmp_path / "legacy"), "--scope", "narration",
                 "--save-id", "save_02", "--narration-style", "travelogue",
                 "--run-id", "upgraded", "--quiet"]) == 0
    new = workflow.load(tmp_path / "upgraded")
    assert new.saves[1].travelogue.selected
    for i in (0, 2, 3):
        assert new.saves[i] == old.saves[i]
    assert new.saves[1].world == old.saves[1].world


def test_export_records_all_candidates_scores_selection_and_prompt_version(generated):
    _, _, run, directory = generated
    manifest = RunStore(directory).load_manifest()
    versions = {p.name: p.version for p in manifest.story_generation.templates}
    assert versions["story_world_simulation"] == "v3"
    assert versions["story_narration_candidates"] == "v1"
    assert versions["story_narration_selection"] == "v1"
    for save in manifest.saves:
        for key in ("narration_candidates", "narration_selection", "narration_travelogue"):
            assert (directory / save.artifacts[key].path).exists()
    text = review_text(run)
    assert "THREE CANDIDATES" in text and '"spoken_flow"' in text
    assert "Selected narration: narration_3" in text
    assert run.saves[0].travelogue.selected.paragraph in text


@pytest.mark.parametrize("stage,field", [
    ("narration_candidates", "candidates"), ("narration_selection", "review")
])
def test_sdk_structured_output_and_medium_effort_over_mock_http(generated, stage, field):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    value = getattr(generated[2].saves[0].travelogue, field)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 0,
            "status": "completed", "model": "gpt-6-luna",
            "output": [{"id": "msg_offline", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": value.model_dump_json(), "annotations": []}]}],
        })

    with openai.OpenAI(api_key="offline-test", max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        actual = OpenAIStoryProvider(StorySettings(), client=client).generate(
            stage=stage, prompt="Offline test", context={}, response_type=type(value)
        )
    assert actual == value
    assert bodies[0]["reasoning"]["effort"] == "medium"
    assert bodies[0]["text"]["format"]["strict"] is True


def test_review_schema_keeps_required_grounding_and_style_fields(generated):
    raw = generated[2].saves[0].travelogue.review.model_dump()
    raw["ratings"][0].pop("grounded")
    with pytest.raises(ValidationError):
        TravelogueReview.model_validate(raw)
