"""Validated creative contracts and checkpoints; no media or timing dependencies."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.broll_beats import BrollBeat, validate_beats
from save_reel.models import (
    CompiledPrompt,
    ConceptRequest,
    Model,
    RunId,
    SaveId,
    StageStatus,
    TemplateReference,
    Text,
    utc_now,
)
from save_reel.story_narration_models import TraveloguePolicy, TravelogueState
from save_reel.story_tiers import TIERS, SurvivabilityTier


class SaveRole(StrEnum):
    ATTRACTION = "attractive_hidden_cost"
    COMFORT = "nostalgic_hard_survival"
    MYSTERY = "inexplicable_mystery"
    DANGER = "openly_ominous"


ROLES = tuple(SaveRole)
SAVE_IDS = ("save_01", "save_02", "save_03", "save_04")


def normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def word_count(lines: tuple[str, ...]) -> int:
    return len(re.findall(r"\b[\w]+(?:['’-][\w]+)*\b", " ".join(lines)))


class NarrationPolicy(Model):
    min_lines: int = Field(default=4, ge=2, le=10)
    max_lines: int = Field(default=6, ge=2, le=12)
    target_min_words: int = Field(default=25, ge=10)
    target_max_words: int = Field(default=40, ge=10)
    word_tolerance: float = Field(default=0.25, ge=0, le=0.5)
    estimated_words_per_minute: int = Field(default=195, ge=60, le=300)

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.min_lines > self.max_lines or self.target_min_words > self.target_max_words:
            raise ValueError("narration minimums must not exceed maximums")
        return self

    def check(self, lines: tuple[str, ...]) -> None:
        if not self.min_lines <= len(lines) <= self.max_lines:
            raise ValueError(f"narration needs {self.min_lines}–{self.max_lines} lines")
        count = word_count(lines)
        if not (
            self.target_min_words * (1 - self.word_tolerance)
            <= count
            <= self.target_max_words * (1 + self.word_tolerance)
        ):
            raise ValueError(
                f"narration has {count} words; target "
                f"{self.target_min_words}–{self.target_max_words} "
                f"with {self.word_tolerance:.0%} tolerance"
            )
        if len({normalized(line) for line in lines}) != len(lines):
            raise ValueError("narration lines must not repeat")


class NoveltyPolicy(Model):
    recent_exact_history: int = Field(default=50, ge=0, le=500)
    recent_semantic_history: int = Field(default=100, ge=0, le=500)
    title_history_window: int = Field(default=50, ge=0, le=500)
    title_suffix_limit: int = Field(default=1, ge=1)
    title_token_limit: int = Field(default=3, ge=1)
    title_phrase_limit: int = Field(default=1, ge=1)
    palette_limit: int = Field(default=2, ge=1)
    similarity_threshold: float = Field(default=0.78, gt=0, le=1)
    minimum_novelty: float = Field(default=0.70, ge=0, le=1)
    minimum_coherence: float = Field(default=0.75, ge=0, le=1)
    minimum_overall: float = Field(default=0.72, ge=0, le=1)
    minimum_reel_diversity: float = Field(default=0.65, ge=0, le=1)
    max_candidate_rounds: int = Field(default=3, ge=1, le=5)
    max_reel_replacements: int = Field(default=8, ge=0, le=20)
    summary_set_limit: int = Field(default=12, ge=1, le=50)

    @property
    def history_count(self) -> int:
        return max(
            self.recent_exact_history, self.recent_semantic_history, self.title_history_window
        )


class StorySettings(Model):
    world_review_prompt_version: str | None = Field(default="v4", pattern=r"^v[1-9][0-9]*$")
    broll_prompt_version: str | None = Field(default="v1", pattern=r"^v[1-9][0-9]*$")
    tier_prompt_version: str | None = Field(default="v1", pattern=r"^v[1-9][0-9]*$")
    model: Text = "gpt-6-luna"
    narration_model: Text = "gpt-6.1-sol"
    prompt_version: str = Field(default="v3", pattern=r"^v[1-9][0-9]*$")
    seed_catalog_version: str = Field(default="v2", pattern=r"^v[1-9][0-9]*$")
    candidates_per_save: int = Field(default=3, ge=2, le=6)
    recent_history_count: int = Field(default=80, ge=0, le=500)
    validation_retries: int = Field(default=2, ge=0, le=10)
    max_world_replacements: int = Field(default=1, ge=0, le=3)
    max_output_tokens: int = Field(default=32000, ge=1000, le=64000)
    world_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    world_review_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = (
        "medium"
    )
    candidate_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    narration_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    narration: NarrationPolicy = Field(default_factory=NarrationPolicy)
    # Independent of the world prompts and legacy briefing policy.
    travelogue: TraveloguePolicy | None = Field(default_factory=TraveloguePolicy.sol)
    novelty: NoveltyPolicy = Field(default_factory=NoveltyPolicy)

    @model_validator(mode="after")
    def compatible_versions(self):
        if (self.prompt_version == "v1") != (self.seed_catalog_version == "v1"):
            raise ValueError("v1 prompts and v1 seeds must be selected together")
        return self

    def effort_for(self, stage: str) -> str | None:
        if self.prompt_version in ("v1", "v2"):
            return None  # Keep the request parameters of legacy workflows.
        if stage == "candidates" and self.candidate_reasoning_effort is not None:
            return self.candidate_reasoning_effort
        if stage == "world_review" and self.world_review_reasoning_effort is not None:
            return self.world_review_reasoning_effort
        return (
            self.narration_reasoning_effort
            if stage.startswith("narration")
            else self.world_reasoning_effort
        )

    def model_for(self, stage: str) -> str:
        if stage in (
            "narration_description", "narration_prose_candidates", "narration_prose_candidate",
            "narration_prose_selection",
        ):
            return self.narration_model
        return self.model


class BroadWorldSeed(Model):
    """V2 constraints intentionally contain no final creative mechanics."""

    role: SaveRole
    survivability_tier: SurvivabilityTier | None = None
    setting_family: Text
    surface_emotion: Text
    deeper_emotion: Text
    resource_pressure: Text
    severity: int = Field(ge=1, le=10)
    era: Text
    time_of_day: Text | None = None
    weather: Text | None = None


class WorldSeed(Model):
    """V1 checkpoint compatibility only; new runs use BroadWorldSeed."""

    role: SaveRole
    setting_family: Text
    specific_setting: Text
    era: Text
    time_of_day: Text
    weather: Text
    architecture_style: Text
    dominant_material: Text
    visual_motif: Text
    surface_emotion: Text
    deeper_emotion: Text
    social_state: Literal["alone", "small_human_group", "nonhuman_workers", "distant_neighbors"]
    main_resource_problem: Text
    danger_type: Text
    impossible_property: Text
    survival_rule_type: Text
    long_term_cost_type: Text
    palette: Annotated[tuple[Text, ...], Field(min_length=2, max_length=5)]
    severity: int = Field(ge=1, le=10)


class SurvivalCandidate(Model):
    candidate_id: Text
    title: Text
    premise: Text
    surface_attraction: Text
    resource_situation: Text
    safe_sleeping: Text
    inhabitants: Text
    primary_danger: Text
    danger_type: Text
    critical_rule: Text
    rule_consequence: Text
    escape: Text
    long_term_drawback: Text
    long_term_cost_type: Text
    mundane_detail: Text
    unanswered_mystery: Text
    visual_hook: Text
    severity: int = Field(ge=1, le=10)


class ConceptSignature(Model):
    setting: Text
    danger: Text
    rule: Text
    cost: Text
    anomaly: Text
    inhabitants: Text


class CausalLogic(Model):
    danger_origin: Text
    rule_protection: Text
    failure_consequence: Text
    cost_origin: Text
    reason_to_choose: Text


class WorldConcept(SurvivalCandidate):
    """Luna authors these details together, rather than filling procedural banks."""

    specific_setting: Text
    survivability_tier: SurvivabilityTier | None = None
    resource_problem: Text
    central_mechanic: Text
    emotional_mechanic: Text
    architecture_style: Text
    dominant_material: Text
    time_of_day: Text
    weather: Text
    anomaly: Text
    palette: Annotated[tuple[Text, ...], Field(min_length=3, max_length=5)]
    palette_reason: Text
    signature: ConceptSignature
    causal_logic: CausalLogic


class CandidateSet(Model):
    candidates: Annotated[tuple[SurvivalCandidate, ...], Field(min_length=2, max_length=6)]

    @model_validator(mode="after")
    def unique_candidates(self):
        for field in ("candidate_id", "title"):
            if len({normalized(getattr(c, field)) for c in self.candidates}) != len(
                self.candidates
            ):
                raise ValueError(f"candidate {field} values must be unique")
        return self


class WorldCandidateSet(CandidateSet):
    candidates: Annotated[tuple[WorldConcept, ...], Field(min_length=2, max_length=6)]


Score = Annotated[int, Field(ge=0, le=5)]


class CandidateRating(Model):
    candidate_id: Text
    concrete_daily_life: Score
    memorable_rule: Score
    originality: Score
    visual_potential: Score
    reason_to_choose: Score
    reject: bool
    rationale: Text

    @property
    def total(self) -> int:
        return sum(
            (
                self.concrete_daily_life,
                self.memorable_rule,
                self.originality,
                self.visual_potential,
                self.reason_to_choose,
            )
        )


class CandidateReview(Model):
    ratings: tuple[CandidateRating, ...]


UnitScore = Annotated[float, Field(ge=0, le=1)]


class WorldCandidateRating(Model):
    candidate_id: Text
    novelty: UnitScore
    causal_coherence: UnitScore
    survival_specificity: UnitScore
    visual_hook: UnitScore
    choice_appeal: UnitScore
    mystery: UnitScore
    non_poetic_writing: UnitScore
    overall: UnitScore
    nearest_history_id: Text | None
    history_similarity: UnitScore
    rejection_reason: Text | None
    rationale: Text
    tier_fit: bool = True


class WorldCandidateReview(Model):
    ratings: tuple[WorldCandidateRating, ...]


class CandidateAssessment(Model):
    candidate_id: Text
    accepted: bool
    novelty: UnitScore
    similarity: UnitScore
    nearest_history_id: Text | None = None
    nearest_title: Text | None = None
    reasons: tuple[Text, ...] = ()


class CandidateAttempt(Model):
    round_number: int
    candidates: WorldCandidateSet
    review: WorldCandidateReview | None
    assessments: dict[str, CandidateAssessment]
    reason: Text


class ReelIssue(Model):
    save_ids: Annotated[tuple[SaveId, ...], Field(min_length=2, max_length=4)]
    dimension: Literal[
        "environment", "resource", "threat", "rule", "inhabitants", "cost", "emotion", "palette"
    ]
    reason: Text


class ReelDiversityReview(Model):
    environment: UnitScore
    resource: UnitScore
    threat: UnitScore
    rule: UnitScore
    inhabitants: UnitScore
    cost: UnitScore
    emotion: UnitScore
    palette: UnitScore
    overall: UnitScore
    outcome_spread: UnitScore = 1
    issues: tuple[ReelIssue, ...]
    weakest_save_id: SaveId | None
    rationale: Text


class ReelReviewAttempt(Model):
    selection: dict[SaveId, Text]
    review: ReelDiversityReview


class SurvivalDetails(Model):
    # Irrelevant details may be omitted or null. The rule and permanent cost are essential.
    food: Text | None = None
    water: Text | None = None
    shelter: Text | None = None
    inhabitants: Text | None = None
    main_threat: Text
    critical_rule: Text
    rule_consequence: Text
    escape: Text | None = None
    mundane_detail: Text | None = None
    long_term_cost: Text


class SurvivalBrief(Model):
    survivability_tier: SurvivabilityTier | None = None
    title: Text
    premise: Text
    surface_promise: Text
    survival: SurvivalDetails
    visual_hook: Text
    unanswered_mystery: Text
    severity: int = Field(ge=1, le=10)
    narration: Annotated[tuple[Text, ...], Field(min_length=1, max_length=12)]


class CartridgeVariables(Model):
    shell_material_color: Text
    shape_language: Text
    motifs: Text
    object_mood: Text
    hint_scene_description: Text


class EnvironmentVariables(Model):
    environment_description: Text
    time_of_day: Text
    key_visual_elements: Annotated[tuple[Text, ...], Field(min_length=1)]
    palette: Annotated[tuple[Text, ...], Field(min_length=2)]
    mood: Text
    visual_anomaly: Text
    environmental_motion: Text
    shot_composition: Text
    key_surfaces: Annotated[tuple[Text, ...], Field(min_length=1)]
    broll_beats: Annotated[tuple[BrollBeat, ...], Field(min_length=2, max_length=2)] | None = None

    @model_validator(mode="after")
    def distinct_broll_beats(self):
        validate_beats(self.broll_beats)
        return self


class FinalStory(Model):
    brief: SurvivalBrief
    cartridge: CartridgeVariables
    environment: EnvironmentVariables


class WorldSpec(Model):
    """Approved simulation facts. No narration or image-prompt prose."""

    environment: Text
    resource_system: Text
    food: Text | None
    water: Text | None
    shelter: Text | None
    inhabitants: Text | None
    threat: Text
    warning_signs: tuple[Text, ...]
    critical_survival_rule: Text
    consequence_if_broken: Text
    escape_conditions: Text | None
    daily_routine: Text
    long_term_cost: Text
    causal_logic: CausalLogic

    def facts(self) -> dict[str, str]:
        result = self.model_dump(exclude={"causal_logic", "warning_signs"}, exclude_none=True)
        if self.warning_signs:
            result["warning_signs"] = " ".join(self.warning_signs)
        return result


WorldFact = Literal[
    "environment", "resource_system", "food", "water", "shelter", "inhabitants", "threat",
    "warning_signs",
    "critical_survival_rule", "consequence_if_broken", "escape_conditions", "daily_routine",
    "long_term_cost",
]


class WorldSimulation(Model):
    spec: WorldSpec
    cartridge: CartridgeVariables
    environment: EnvironmentVariables


class WorldApproval(Model):
    causal_coherence: UnitScore
    preserves_selected_concept: bool
    practical_system: bool
    issues: tuple[Text, ...]

    def approved(self, policy: NoveltyPolicy) -> bool:
        return (
            self.causal_coherence >= policy.minimum_coherence
            and self.preserves_selected_concept and self.practical_system and not self.issues
        )


class GroundedLine(Model):
    text: Text
    fact_refs: Annotated[tuple[WorldFact, ...], Field(min_length=1)]
    tone: Literal["practical", "unsettling"]
    practical_change: Text


class GroundedNarration(Model):
    lines: Annotated[tuple[GroundedLine, ...], Field(min_length=2, max_length=12)]

    @property
    def narration(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines)

    def check(self, spec: WorldSpec, policy: NarrationPolicy) -> None:
        policy.check(self.narration)
        unsettling = sum(line.tone == "unsettling" for line in self.lines)
        if not 1 <= unsettling <= 2 or unsettling >= len(self.lines) - unsettling:
            raise ValueError("narration needs a practical majority and only 1–2 unsettling lines")
        facts = spec.facts()
        for line in self.lines:
            if len(set(line.fact_refs)) != len(line.fact_refs):
                raise ValueError("line fact references must be distinct")
            if any(ref not in facts for ref in line.fact_refs):
                raise ValueError("narration refers to a missing or unknown world fact")


class NarrationLineReview(Model):
    line_number: int = Field(ge=1, le=12)
    supported_by_facts: bool
    changes_practical_understanding: bool
    repeats_information: bool
    tone: Literal["practical", "unsettling"]
    issue: Text | None


class GroundingReview(Model):
    lines: tuple[NarrationLineReview, ...]

    def check(self, draft: GroundedNarration) -> None:
        if tuple(line.line_number for line in self.lines) != tuple(range(1, len(draft.lines) + 1)):
            raise ValueError("grounding review must assess each narration line once in order")

    def problems(self, draft: GroundedNarration) -> tuple[str, ...]:
        self.check(draft)
        return tuple(
            f"Line {item.line_number}: "
            f"{item.issue or 'unsupported, redundant or non-practical line'}"
            for line, item in zip(draft.lines, self.lines, strict=True)
            if not item.supported_by_facts or not item.changes_practical_understanding
            or item.repeats_information or item.tone != line.tone or item.issue is not None
        )


class WorldAttempt(Model):
    simulation: WorldSimulation
    review: WorldApproval


class WorldReplacement(Model):
    concept: WorldConcept
    candidate_round: int = Field(ge=1)
    attempts: tuple[WorldAttempt, ...]
    pending_simulation: WorldSimulation | None = None
    reason: Text


class WorldReviewUpdate(Model):
    created_at: datetime = Field(default_factory=utc_now)
    previous_prompt: CompiledPrompt
    previous_effort: Text | None
    new_prompt: CompiledPrompt
    new_effort: Text
    attempts: dict[SaveId, tuple[WorldAttempt, ...]]


class NarrationAttempt(Model):
    draft: GroundedNarration
    review: GroundingReview


class NarrationDraft(Model):
    narration: tuple[Text, ...]


class NarrationStats(Model):
    words: int
    lines: int
    estimated_seconds: float
    within_target: bool

    @classmethod
    def measure(cls, lines: tuple[str, ...], policy: NarrationPolicy):
        policy.check(lines)
        count = word_count(lines)
        return cls(
            words=count,
            lines=len(lines),
            estimated_seconds=round(count / policy.estimated_words_per_minute * 60, 2),
            within_target=policy.target_min_words <= count <= policy.target_max_words,
        )


class HistoryEntry(Model):
    fingerprint: Text
    created_at: datetime
    run_id: RunId
    save_id: SaveId
    title: Text
    setting_family: Text
    specific_setting: Text
    visual_hook: Text
    danger_type: Text
    survival_rule: Text
    resource_problem: Text
    long_term_cost_type: Text
    long_term_cost: Text
    inhabitants: Text | None
    anomaly: Text
    palette: tuple[Text, ...]
    emotions: tuple[Text, ...]
    primary_danger: Text | None = None
    central_mechanic: Text | None = None
    emotional_mechanic: Text | None = None
    signature: ConceptSignature | None = None


class StorySave(Model):
    save_id: SaveId
    seed: BroadWorldSeed | WorldSeed
    candidates: WorldCandidateSet | CandidateSet | None = None
    review: WorldCandidateReview | CandidateReview | None = None
    selected: WorldConcept | SurvivalCandidate | None = None
    selection_score: float | None = None
    final: FinalStory | None = None
    narration_stats: NarrationStats | None = None
    candidate_round: int = Field(default=1, ge=1)
    assessments: dict[str, CandidateAssessment] = Field(default_factory=dict)
    previous_attempts: tuple[CandidateAttempt, ...] = ()
    excluded_candidates: tuple[Text, ...] = ()
    replacement_feedback: tuple[Text, ...] = ()
    world: WorldSimulation | None = None
    world_review: WorldApproval | None = None
    world_attempts: tuple[WorldAttempt, ...] = ()
    world_replacements: tuple[WorldReplacement, ...] = ()
    narration_draft: GroundedNarration | None = None
    grounding_review: GroundingReview | None = None
    narration_attempts: tuple[NarrationAttempt, ...] = ()
    travelogue: TravelogueState | None = None
    # A narration-only rewrite may use a different provider from the original world.
    narration_provider: Text | None = None
    narration_model: Text | None = None

    def spoken_policy(self, settings):
        return self.travelogue.policy if self.travelogue else settings.narration


class StoryRun(Model):
    schema_version: Literal["1.0", "2.0", "3.0"] = "3.0"
    kind: Literal["survival_story_generation"] = "survival_story_generation"
    run_id: RunId
    source_run_id: RunId | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    request: ConceptRequest
    provider: Text
    model: Text
    settings: StorySettings
    random_seed: int
    seed_catalog: TemplateReference
    templates: dict[str, CompiledPrompt]
    history: tuple[HistoryEntry, ...] = ()
    saves: Annotated[tuple[StorySave, ...], Field(min_length=4, max_length=4)]
    status: StageStatus = StageStatus.PENDING
    error: Text | None = None
    regeneration: Literal["reel", "save", "candidates", "narration"] | None = None
    regeneration_save_id: SaveId | None = None
    narration_pending: bool = False
    reel_review: ReelDiversityReview | None = None
    reel_review_history: tuple[ReelReviewAttempt, ...] = ()
    reel_replacements: int = 0
    frozen_save_ids: tuple[SaveId, ...] = ()
    world_review_updates: tuple[WorldReviewUpdate, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def preserve_saved_narration_policy(cls, value):
        # Missing in old checkpoints: resume their saved briefing behavior, never upgrade it.
        if isinstance(value, dict) and isinstance(value.get("settings"), dict):
            value = {**value, "settings": dict(value["settings"])}
            value["settings"].setdefault("tier_prompt_version", None)
            value["settings"].setdefault("broll_prompt_version", None)
            value["settings"].setdefault("world_review_prompt_version", None)
            value["settings"].setdefault("world_review_reasoning_effort", None)
            if "travelogue" not in value["settings"]:
                value = {**value, "settings": {**value["settings"], "travelogue": None}}
        return value

    @model_validator(mode="after")
    def diversity(self):
        if tuple(s.save_id for s in self.saves) != SAVE_IDS:
            raise ValueError("story requires exactly four ordered distinct save IDs")
        if tuple(s.seed.role for s in self.saves) != ROLES:
            raise ValueError("each save must occupy its distinct narrative role")
        tiers = [getattr(s.seed, "survivability_tier", None) for s in self.saves]
        if all(tiers) and set(tiers) != set(TIERS):
            raise ValueError("a tiered reel needs one best, good, risky and bad save")
        is_v2 = self.schema_version != "1.0"
        fields = (
            ("setting_family",)
            if is_v2
            else ("setting_family", "danger_type", "long_term_cost_type")
        )
        if any(isinstance(s.seed, BroadWorldSeed) != is_v2 for s in self.saves):
            raise ValueError("seed schema does not match story version")
        for field in fields:
            if len({normalized(getattr(s.seed, field)) for s in self.saves}) != 4:
                raise ValueError(f"four distinct {field} values are required")
        if not is_v2 and all(s.seed.social_state == "alone" for s in self.saves):
            raise ValueError("the saves cannot all be solitary")
        chosen = [s.selected for s in self.saves if s.selected]
        if len({normalized(c.title) for c in chosen}) != len(chosen) and (
            not is_v2 or self.status == StageStatus.COMPLETED
        ):
            raise ValueError("selected titles must be unique within a reel")
        for slot in self.saves:
            if self.schema_version == "3.0":
                from save_reel.story_simulation import validate_slot

                validate_slot(self, slot)
            if slot.candidates:
                candidates = slot.candidates.candidates
                expected = {
                    f"{slot.save_id}_candidate_{i}"
                    for i in range(1, self.settings.candidates_per_save + 1)
                }
                if {c.candidate_id for c in candidates} != expected:
                    raise ValueError("cached candidate IDs/count do not match settings")
                if is_v2 and not isinstance(slot.candidates, WorldCandidateSet):
                    raise ValueError("v2 needs complete world candidates")
                if not is_v2 and any(
                    c.danger_type != slot.seed.danger_type
                    or c.long_term_cost_type != slot.seed.long_term_cost_type
                    for c in candidates
                ):
                    raise ValueError("cached candidate categories must match the seed")
            if slot.review:
                if is_v2 and not isinstance(slot.review, WorldCandidateReview):
                    raise ValueError("v2 requires novelty and causal-coherence scores")
                if not slot.candidates:
                    raise ValueError("a review requires cached candidates")
                ids = [r.candidate_id for r in slot.review.ratings]
                if len(ids) != len(expected) or set(ids) != expected:
                    raise ValueError("cached review must rate every candidate exactly once")
            if slot.selected:
                if getattr(slot.seed, "survivability_tier", None) and (
                    slot.selected.survivability_tier != slot.seed.survivability_tier
                ):
                    raise ValueError("selected world must match its assigned survivability tier")
                if slot.candidates is None or slot.selected not in slot.candidates.candidates:
                    raise ValueError("selected concept must come from the saved candidate set")
                if not slot.review or not any(
                    r.candidate_id == slot.selected.candidate_id
                    and (r.rejection_reason is None if is_v2 else not r.reject)
                    for r in slot.review.ratings
                ):
                    raise ValueError("selected concept must have an eligible critic review")
                if not is_v2 and (
                    slot.selected.danger_type != slot.seed.danger_type
                    or slot.selected.long_term_cost_type != slot.seed.long_term_cost_type
                ):
                    raise ValueError("selected danger/cost types must match the seed")
            if slot.final:
                if getattr(slot.selected, "survivability_tier", None) and (
                    slot.final.brief.survivability_tier != slot.selected.survivability_tier
                ):
                    raise ValueError("final brief must preserve the survivability tier")
                if not slot.selected or slot.final.brief.title != slot.selected.title:
                    raise ValueError("final title must match the selected concept")
                slot.spoken_policy(self.settings).check(slot.final.brief.narration)
                for field, value in (
                    ("main_threat", slot.selected.primary_danger),
                    ("critical_rule", slot.selected.critical_rule),
                    ("rule_consequence", slot.selected.rule_consequence),
                    ("long_term_cost", slot.selected.long_term_drawback),
                ):
                    if getattr(slot.final.brief.survival, field) != value:
                        raise ValueError("cached brief must preserve the selected survival facts")
                if is_v2 and (
                    slot.final.environment.palette != slot.selected.palette
                    or slot.final.environment.visual_anomaly != slot.selected.anomaly
                ):
                    raise ValueError("final visuals must preserve the selected palette and anomaly")
        if self.status == StageStatus.COMPLETED and not all(s.final for s in self.saves):
            raise ValueError("completed story runs require four final saves")
        if self.status == StageStatus.COMPLETED:
            if is_v2:
                from save_reel.story_novelty import reel_issues, reel_review_passes

                if not self.reel_review or not reel_review_passes(
                    self.reel_review, self.settings.novelty
                ):
                    raise ValueError("completed v2 stories need an approved reel diversity review")
                if reel_issues(self.saves, self.settings.novelty):
                    raise ValueError("completed reel fails deterministic diversity checks")
                for slot in self.saves:
                    assessment = slot.assessments.get(slot.selected.candidate_id)
                    if not assessment or not assessment.accepted:
                        raise ValueError("completed stories need accepted novelty assessments")
                    rating = next(
                        r
                        for r in slot.review.ratings
                        if r.candidate_id == slot.selected.candidate_id
                    )
                    policy = self.settings.novelty
                    if (
                        rating.novelty < policy.minimum_novelty
                        or not rating.tier_fit
                        or rating.causal_coherence < policy.minimum_coherence
                        or rating.overall < policy.minimum_overall
                        or rating.history_similarity >= policy.similarity_threshold
                    ):
                        raise ValueError("completed stories must meet critic thresholds")
            for slot in self.saves:
                expected_stats = NarrationStats.measure(
                    slot.final.brief.narration, slot.spoken_policy(self.settings)
                )
                if slot.narration_stats != expected_stats:
                    raise ValueError("cached narration statistics do not match the spoken text")
        return self
