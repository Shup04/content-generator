"""V2 quality gates and compatibility; all generation uses local fakes."""

import copy
import hashlib
import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from save_reel.cli import main
from save_reel.models import ConceptRequest, utc_now
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import review_text, visual_values
from save_reel.story_history import StoryHistory, creative_history
from save_reel.story_models import (
    BroadWorldSeed,
    ConceptSignature,
    HistoryEntry,
    NoveltyPolicy,
    ReelIssue,
    StoryRun,
    WorldCandidateRating,
    WorldCandidateReview,
    WorldCandidateSet,
    WorldConcept,
)
from save_reel.story_models import (
    StorySettings as CurrentStorySettings,
)
from save_reel.story_novelty import assess_candidate, palette_key, reel_issues
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prompting import load_prompts, render_request
from save_reel.story_seeds import load_catalog, make_seeds


class StorySettings(CurrentStorySettings):
    prompt_version: str = "v2"


@pytest.fixture(autouse=True)
def legacy_v2_defaults(monkeypatch):
    monkeypatch.setattr("save_reel.story_pipeline.StorySettings", StorySettings)
    monkeypatch.setattr("save_reel.story_cli.StorySettings", StorySettings)


class SpyProvider(MockStoryProvider):
    def __init__(self):
        super().__init__(version="v2")
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super().generate(**kwargs)


def concept(index=0):
    raw = json.loads(
        files("save_reel.prompt_templates").joinpath("story_fixtures", "v2.json").read_text()
    )[index]["concept"]
    return WorldConcept.model_validate(raw)


def entry(c, number=0):
    return HistoryEntry(
        fingerprint=hashlib.sha256((c.title + str(number)).encode()).hexdigest(),
        created_at=utc_now(),
        run_id=f"previous-{number}",
        save_id="save_01",
        title=c.title,
        setting_family="maritime",
        specific_setting=c.specific_setting,
        visual_hook=c.visual_hook,
        danger_type=c.danger_type,
        primary_danger=c.primary_danger,
        survival_rule=c.critical_rule,
        resource_problem=c.resource_problem,
        long_term_cost_type=c.long_term_cost_type,
        long_term_cost=c.long_term_drawback,
        inhabitants=c.inhabitants,
        anomaly=c.anomaly,
        palette=c.palette,
        emotions=("comfort", "dread"),
        signature=c.signature,
        central_mechanic=c.central_mechanic,
        emotional_mechanic=c.emotional_mechanic,
    )


def rating(c, **changes):
    fields = dict(
        candidate_id=c.candidate_id,
        novelty=0.9,
        causal_coherence=0.9,
        survival_specificity=0.9,
        visual_hook=0.9,
        choice_appeal=0.9,
        mystery=0.9,
        non_poetic_writing=0.9,
        overall=0.9,
        nearest_history_id=None,
        history_similarity=0,
        rejection_reason=None,
        rationale="A coherent system.",
    )
    return WorldCandidateRating(**(fields | changes))


@pytest.fixture
def generated(tmp_path):
    provider = SpyProvider()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="v2", random_seed=42)
    return workflow, provider, run, tmp_path / "v2"


@pytest.mark.parametrize(
    "field", ["anomaly", "critical_rule", "long_term_drawback", "specific_setting", "title"]
)
def test_exact_reuse_is_rejected_despite_different_other_fields(field):
    previous, new = concept(0), concept(8)
    setattr(new, field, getattr(previous, field).upper() + "!!")
    decision = assess_candidate(new, (entry(previous),), NoveltyPolicy(), rating(new))
    assert not decision.accepted
    assert any(f"exact {field}" in reason for reason in decision.reasons)


@pytest.mark.parametrize("suffix", ["ANNEX", "ROOM", "DUTY", "RIDGE"])
def test_title_suffix_limits_are_generic_and_configurable(suffix):
    old, new = concept(0), concept(8)
    old.title, new.title = f"QUIET {suffix}", f"PURPLE {suffix}"
    history = (entry(old),)
    result = assess_candidate(new, history, NoveltyPolicy())
    assert not result.accepted
    assert any("title suffix" in reason for reason in result.reasons)
    assert assess_candidate(new, history, NoveltyPolicy(title_suffix_limit=2)).accepted


def test_title_roots_cannot_be_reused_with_fresh_suffixes():
    old, new = concept(0), concept(8)
    old.title, new.title = "DAWN BATCH", "DAWN BATCH ANNEX"
    result = assess_candidate(new, (entry(old),), NoveltyPolicy())
    assert any("title phrase" in reason for reason in result.reasons)


def test_paraphrased_bakery_mechanic_is_rejected():
    previous, new = concept(0), concept(8)
    previous.signature = ConceptSignature(
        setting="automated bakery sleeping quarters",
        danger="workers guard the ration counter",
        rule="one meal arrives daily",
        cost="permanent ration dependence",
        anomaly="machines bake without power",
        inhabitants="metal workers ignore you",
    )
    new.signature = ConceptSignature(
        setting="underground bakery",
        danger="staff guard your daily ration",
        rule="one ration arrives daily",
        cost="dependent on daily ration",
        anomaly="ovens work with no electricity",
        inhabitants="workers never speak",
    )
    result = assess_candidate(new, (entry(previous),), NoveltyPolicy())
    assert not result.accepted
    assert result.similarity >= 0.78
    assert result.nearest_title == previous.title
    assert any("structural similarity" in reason for reason in result.reasons)


def test_model_semantic_review_can_reject_beyond_word_overlap():
    previous, new = concept(0), concept(8)
    prior = entry(previous)
    assert assess_candidate(new, (prior,), NoveltyPolicy()).similarity < 0.78
    score = rating(new, nearest_history_id=prior.fingerprint, history_similarity=0.91)
    result = assess_candidate(new, (prior,), NoveltyPolicy(), score)
    assert not result.accepted
    assert result.nearest_title == prior.title
    assert result.similarity == 0.91


@pytest.mark.parametrize("score", ["novelty", "causal_coherence", "overall"])
def test_required_scores_and_thresholds(score):
    c = concept()
    data = rating(c).model_dump()
    del data[score]
    with pytest.raises(ValidationError):
        WorldCandidateRating.model_validate(data)
    result = assess_candidate(c, (), NoveltyPolicy(), rating(c, **{score: 0.1}))
    assert not result.accepted
    assert any("below minimum" in reason for reason in result.reasons)


def test_palette_is_authored_and_frequency_is_order_independent():
    old, new = concept(0), concept(8)
    old.palette = ("oxidized silver", "berry ink", "warm chalk")
    new.palette = tuple(reversed(old.palette))
    result = assess_candidate(new, (entry(old), entry(old, 1)), NoveltyPolicy())
    assert not result.accepted
    assert "palette combination reached recent frequency limit" in result.reasons
    new.palette = ("#40302a", "fern shadow", "sun-bleached peach")
    assert assess_candidate(new, (entry(old),), NoveltyPolicy()).accepted
    palette_schema = WorldConcept.model_json_schema()["properties"]["palette"]["items"]
    assert "enum" not in palette_schema
    assert palette_key(old.palette) != palette_key(new.palette)


def test_broad_seeds_contain_no_final_details_and_remain_reproducible():
    seeds, reference = make_seeds(33, StorySettings(), ())
    assert (seeds, reference) == make_seeds(33, StorySettings(), ())
    forbidden = {
        "specific_setting",
        "palette",
        "anomaly",
        "impossible_property",
        "survival_rule_type",
        "danger_type",
        "social_state",
        "inhabitants",
        "long_term_cost_type",
        "visual_motif",
        "title",
    }
    assert all(isinstance(seed, BroadWorldSeed) for seed in seeds)
    assert all(not forbidden & type(seed).model_fields.keys() for seed in seeds)
    catalog, _ = load_catalog("v2")
    assert (
        not {"hazards", "costs", "palettes", "impossible_properties", "settings"} & catalog.keys()
    )
    assert len({s.setting_family for s in seeds}) == 4


def test_compact_history_has_bounded_sets_windows_and_no_narration(generated):
    run = generated[2]
    history = tuple(entry(s.selected, i) for i, s in enumerate(run.saves))
    policy = NoveltyPolicy(
        recent_exact_history=2,
        recent_semantic_history=3,
        title_history_window=1,
        summary_set_limit=1,
    )
    summary = creative_history(history, policy)
    assert summary["windows"] == {"exact": 2, "semantic": 3, "titles": 1}
    assert len(summary["recent_settings"]) <= 1
    assert len(summary["recent_save_summaries"]) == 3
    assert len(summary["recent_title_patterns"]["titles"]) == 1
    text = json.dumps(summary)
    assert "narration" not in text
    assert "causal_logic" not in text
    assert len(text) < 5500
    prompt = render_request(
        load_prompts("v2")["candidates"],
        {"seed": run.saves[0].seed.model_dump(mode="json"), "creative_history": summary},
    )
    assert prompt.count("recent_save_summaries") == 1
    assert "RECENT CREATIVE HISTORY" in prompt
    assert "occupied" in prompt


def test_history_is_passed_to_both_generation_and_critic(generated):
    workflow, provider, _, directory = generated
    provider.calls.clear()
    run = workflow.create(
        ConceptRequest(), runs_dir=directory.parent, run_id="next", random_seed=10
    )
    assert run.history
    for call in provider.calls:
        if call["stage"] in ("candidates", "review"):
            assert call["context"]["creative_history"]["recent_save_summaries"]
            assert "recent_history" not in call["context"]
            assert "recent_save_summaries" in call["prompt"]
        elif call["stage"] in ("brief", "narration"):
            assert "recent_save_summaries" not in call["prompt"]


@pytest.mark.parametrize(
    "field",
    ["primary_danger", "central_mechanic", "specific_setting", "palette", "long_term_drawback"],
)
def test_reel_level_exact_diversity_checks(generated, field):
    run = generated[2].model_copy(deep=True)
    setattr(run.saves[1].selected, field, getattr(run.saves[0].selected, field))
    issues = reel_issues(run.saves, run.settings.novelty)
    assert issues
    assert any(set(i.save_ids) == {"save_01", "save_02"} for i in issues)


def test_reel_replaces_redundant_choice_then_reevaluates(tmp_path):
    class RejectOnce(SpyProvider):
        rejected = False

        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"] == "reel_review" and not self.rejected:
                self.rejected = True
                result.overall = 0.4
                result.issues = (
                    ReelIssue(
                        save_ids=("save_01", "save_02"),
                        dimension="emotion",
                        reason="These choices offer the same emotional bargain.",
                    ),
                )
                result.weakest_save_id = "save_02"
            return result

    provider = RejectOnce()
    run = StoryWorkflow(provider).create(ConceptRequest(), runs_dir=tmp_path, run_id="replaced")
    assert run.reel_replacements == 1
    assert len(run.reel_review_history) == 2
    assert run.saves[1].selected.candidate_id == "save_02_candidate_2"
    assert run.saves[1].assessments["save_02_candidate_1"].accepted is False
    assert sum(c["stage"] == "reel_review" for c in provider.calls) == 2


@pytest.mark.parametrize("score", ["novelty", "causal_coherence"])
def test_low_scores_trigger_bounded_regeneration_and_explain_failure(tmp_path, score):
    class BadScores(SpyProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"] == "review":
                for row in result.ratings:
                    setattr(row, score, 0.1)
            return result

    provider = BadScores()
    workflow = StoryWorkflow(provider)
    settings = StorySettings(novelty=NoveltyPolicy(max_candidate_rounds=2))
    with pytest.raises(ValueError, match="no acceptable concepts after 2 rounds"):
        workflow.create(ConceptRequest(), settings=settings, runs_dir=tmp_path, run_id="failed")
    saved = workflow.load(tmp_path / "failed")
    assert len(saved.saves[0].previous_attempts) == 1
    assert saved.saves[0].candidate_round == 2
    assert sum(c["stage"] == "candidates" for c in provider.calls) == 2
    assert provider.calls[-2]["context"]["replacement_feedback"]
    review = (tmp_path / "failed/story_review.txt").read_text()
    assert "Rejection reasons:" in review
    assert "below minimum" in review
    assert "Candidate round 1:" in review and "Candidate round 2:" in review
    assert "Scores:" in review
    assert "Nearest prior save:" in review
    assert not (tmp_path / "failed/.story.lock").exists()


def test_v2_cache_regeneration_and_frozen_saves(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    assert workflow.resume(directory) == old
    assert not provider.calls
    new = workflow.regenerate(directory, scope="candidates", save_id="save_03", run_id="fork")
    assert new.saves[2].selected.title != old.saves[2].selected.title
    for i in (0, 1, 3):
        assert new.saves[i].model_dump_json() == old.saves[i].model_dump_json()
    assert all(
        c["stage"] == "reel_review" or c["context"]["save_id"] == "save_03" for c in provider.calls
    )
    assert sum(c["stage"] == "reel_review" for c in provider.calls) == 1


def test_narration_only_regeneration_preserves_world_and_approval(generated):
    workflow, provider, old, directory = generated
    provider.calls.clear()
    new = workflow.regenerate(directory, scope="narration", save_id="save_02", run_id="spoken")
    assert [c["stage"] for c in provider.calls] == ["narration"]
    assert new.reel_review == old.reel_review
    assert new.saves[1].final.environment == old.saves[1].final.environment
    assert new.saves[1].final.brief.survival == old.saves[1].final.brief.survival
    assert len(StoryHistory(directory.parent / ".story-history").recent(100)) == 4


def test_completed_v2_requires_reel_approval_and_critic_thresholds(generated):
    data = generated[2].model_dump()
    data["reel_review"] = None
    with pytest.raises(ValidationError, match="diversity review"):
        StoryRun.model_validate(data)
    data = generated[2].model_dump()
    for rating_data in data["saves"][0]["review"]["ratings"]:
        rating_data["causal_coherence"] = 0.1
    with pytest.raises(ValidationError, match="critic thresholds"):
        StoryRun.model_validate(data)


def test_history_imports_legacy_manifests_without_regeneration(tmp_path):
    from save_reel.providers.mock_story_v1 import MockStoryProvider as LegacyProvider

    legacy = StoryWorkflow(LegacyProvider()).create(
        ConceptRequest(),
        runs_dir=tmp_path / "runs",
        run_id="old",
        settings=StorySettings(prompt_version="v1", seed_catalog_version="v1"),
    )
    directory = tmp_path / "runs" / legacy.run_id
    # Remove new optional checkpoint keys to exercise the original on-disk shape.
    path = directory / "story.json"
    raw = json.loads(path.read_text())
    raw["settings"].pop("novelty")
    for key in ("reel_review", "reel_review_history", "frozen_save_ids", "reel_replacements"):
        raw.pop(key)
    for save in raw["saves"]:
        for key in (
            "candidate_round",
            "assessments",
            "previous_attempts",
            "excluded_candidates",
            "replacement_feedback",
        ):
            save.pop(key)
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    history = StoryHistory(tmp_path / "imported")
    assert history.import_runs((directory / "manifest.json",)) == (1, 4)
    assert history.import_runs((tmp_path / "runs",)) == (1, 4)
    assert path.read_bytes() == before
    entries = history.recent(100)
    assert all(e.primary_danger and e.survival_rule and e.long_term_cost for e in entries)
    assert entries[0].anomaly
    assert (
        main(["import-story-history", str(directory), "--history-dir", str(tmp_path / "cli-index")])
        == 0
    )


def test_story_debug_and_existing_visual_compilers(generated):
    from save_reel.prompting import (
        BrollStillPromptCompiler,
        BrollVideoPromptCompiler,
        CartridgePromptCompiler,
    )

    _, _, run, directory = generated
    review = review_text(run)
    for phrase in (
        "MOCK FIXTURES",
        "Broad seed:",
        "Candidate round",
        "Scores:",
        "causal_coherence",
        "Rejection reasons:",
        "Novelty:",
        "Nearest prior save:",
        "Selected candidate:",
        "Narration:",
    ):
        assert phrase in review
    for slot in run.saves:
        cart, broll, motion = visual_values(slot)
        assert slot.selected.anomaly == slot.final.environment.visual_anomaly
        assert slot.selected.palette == slot.final.environment.palette
        assert (
            slot.selected.visual_hook
            in CartridgePromptCompiler("v2")
            .render(cart.model_dump(by_alias=True, exclude_none=True))
            .text
        )
        prompt = BrollStillPromptCompiler("v2").render(broll.model_dump(by_alias=True))
        assert slot.selected.anomaly in prompt.text
        assert slot.selected.palette[0] in prompt.text
        assert (
            BrollVideoPromptCompiler("v2")
            .render({**broll.model_dump(by_alias=True), **motion.model_dump(by_alias=True)})
            .text
        )
    assert all(
        t.version == ("v1" if t.name in ("story_tiers", "story_broll_beats") else "v2")
        for t in RunStore(directory).load_manifest().story_generation.templates
    )


def test_current_schema_is_accepted_by_sdk_structured_output_conversion(generated):
    pytest.importorskip("openai")
    from openai.lib._pydantic import to_strict_json_schema

    from save_reel.story_models import ReelDiversityReview

    for response_type in (WorldCandidateSet, WorldCandidateReview, ReelDiversityReview):
        schema = to_strict_json_schema(response_type)
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(schema["required"])
    candidate = generated[2].saves[0].selected
    assert WorldConcept.model_validate_json(candidate.model_dump_json()) == candidate


def test_unknown_semantic_history_match_is_invalid(generated):
    _, _, run, _ = generated
    review = run.saves[0].review.model_copy(deep=True)
    review.ratings[0].nearest_history_id = "invented-history-id"
    with pytest.raises(ValueError, match="identify supplied history") as error:
        StoryWorkflow._check_world_review(run, run.saves[0], review)
    assert "invented-history-id" in str(error.value)
    assert "creative_history.recent_save_summaries" in str(error.value)
    assert "current-reel save_id" in str(error.value)


def test_hard_rejection_regenerates_a_fresh_set_and_retains_diagnostics(tmp_path):
    old = entry(concept(10))
    history = StoryHistory(tmp_path / "history")
    RunStore._write_atomic(
        history.directory / f"{old.fingerprint}.json", old.model_dump_json().encode()
    )

    class RepeatsOnce(SpyProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            context = kwargs["context"]
            if (
                kwargs["stage"] == "candidates"
                and context["save_id"] == "save_01"
                and context["candidate_round"] == 1
            ):
                for c in result.candidates:
                    c.anomaly = old.anomaly
            return result

    provider = RepeatsOnce()
    run = StoryWorkflow(provider, history=history).create(
        ConceptRequest(), runs_dir=tmp_path, run_id="repaired"
    )
    assert run.saves[0].candidate_round == 2
    assert len(run.saves[0].previous_attempts) == 1
    assert all(not a.accepted for a in run.saves[0].previous_attempts[0].assessments.values())
    assert run.saves[0].selected.anomaly != old.anomaly
    assert "exact anomaly" in review_text(run)


def test_resume_reuses_v2_candidates_scores_and_reel_approval(tmp_path):
    class Interrupted(SpyProvider):
        stopped = False

        def generate(self, **kwargs):
            if (
                kwargs["stage"] == "brief"
                and kwargs["context"]["save_id"] == "save_02"
                and not self.stopped
            ):
                self.stopped = True
                raise RuntimeError("test interruption")
            return super().generate(**kwargs)

    provider = Interrupted()
    workflow = StoryWorkflow(provider)
    with pytest.raises(RuntimeError):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="partial")
    old = workflow.load(tmp_path / "partial")
    provider.calls.clear()
    completed = workflow.resume(tmp_path / "partial")
    assert [c["stage"] for c in provider.calls] == ["brief"] * 3
    assert completed.saves[0] == old.saves[0]
    assert completed.reel_review == old.reel_review


def test_deterministic_reel_conflict_replaces_lowest_scoring_involved_save(tmp_path):
    original_rule = concept(0).critical_rule

    class DuplicatedRule(SpyProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            context = kwargs["context"]
            if kwargs["stage"] == "candidates" and context["save_id"] == "save_02":
                result.candidates[0].critical_rule = original_rule
            if kwargs["stage"] == "review" and context["save_id"] == "save_02":
                for i, r in enumerate(result.ratings):
                    r.overall = 0.82 - i * 0.01
            return result

    run = StoryWorkflow(DuplicatedRule()).create(
        ConceptRequest(), runs_dir=tmp_path, run_id="diverse"
    )
    assert run.saves[1].selected.candidate_id == "save_02_candidate_2"
    assert run.saves[1].assessments["save_02_candidate_1"].accepted is False
    assert run.reel_replacements == 1
    assert not reel_issues(run.saves, run.settings.novelty)


def test_reel_critic_replacement_limit_stops_instead_of_spending_indefinitely(tmp_path):
    class NeverAccepts(SpyProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"] == "reel_review":
                result.overall = 0.2
                result.weakest_save_id = "save_01"
                result.rationale = "The selected environments still feel equivalent."
            return result

    provider = NeverAccepts()
    workflow = StoryWorkflow(provider)
    with pytest.raises(ValueError, match="replacement limit"):
        workflow.create(
            ConceptRequest(),
            runs_dir=tmp_path,
            run_id="bounded",
            settings=StorySettings(novelty=NoveltyPolicy(max_reel_replacements=1)),
        )
    assert sum(c["stage"] == "reel_review" for c in provider.calls) == 2
    assert not any(c["stage"] == "brief" for c in provider.calls)
    assert (tmp_path / "bounded/story_review.txt").exists()


def test_exact_and_semantic_windows_are_respected():
    c = concept()
    policy = NoveltyPolicy(
        recent_exact_history=0, recent_semantic_history=0, title_history_window=0
    )
    assert assess_candidate(c, (entry(c),), policy, rating(c)).accepted
    assert creative_history((entry(c),), policy)["recent_save_summaries"] == []


def test_v2_story_only_cli_is_offline_and_no_media_modules_are_imported(tmp_path):
    import subprocess
    import sys

    script = """
import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'openai', 'httpx', 'save_reel.providers.minimax_video',
                         'save_reel.providers.openai_image',
                         'save_reel.providers.elevenlabs_speech',
                         'save_reel.rendering', 'save_reel.narration'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
from save_reel.cli import main
raise SystemExit(main(['generate-concepts', '--provider', 'mock', '--run-id', 'v2', '--quiet']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Mock fixtures only" in result.stderr
    assert (tmp_path / "runs/v2/story_review.txt").exists()


@pytest.mark.parametrize("field", ["visual_anomaly", "palette", "time_of_day"])
def test_final_expansion_cannot_change_an_accepted_world(generated, field):
    run = generated[2]
    final = run.saves[0].final.model_copy(deep=True)
    setattr(
        final.environment, field, ("new", "colors", "here") if field == "palette" else "changed"
    )
    with pytest.raises(ValueError, match="selected"):
        StoryWorkflow._check_final(run, run.saves[0], final)
