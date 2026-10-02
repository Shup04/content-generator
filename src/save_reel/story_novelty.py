"""Explainable reuse gates and local signature similarity, supplemented by the critic."""

import re
from collections import Counter
from itertools import combinations

from save_reel.story_models import (
    CandidateAssessment,
    ConceptSignature,
    HistoryEntry,
    NoveltyPolicy,
    ReelDiversityReview,
    ReelIssue,
    WorldCandidateRating,
    WorldConcept,
    normalized,
)

STOP = set(
    "a an the you your they their it its this that is are was were be to of in on "
    "at by with and or for from every each must can will only has have there here".split()
)
# Small normalization rules, not creative content. The model critic handles wider paraphrases.
ALIASES = (
    (
        r"\b(non[ -]?responsive|unresponsive|never speak|never speaks|do not speak|silent|mute|"
        r"ignore spoken requests|ignore you)\b",
        "unresponsive",
    ),
    (r"\b(one|single) (meal|ration)( a| per)? (day|daily)\b", "daily ration"),
    (r"\b(automated|automatic)\b", "automated"),
    (r"\b(bakehouse|bakery)\b", "bakery"),
    (r"\b(food|meals|meal|rations)\b", "ration"),
    (r"\b(subterranean|underground)\b", "underground"),
    (r"\b(workers|worker|workmen|staff)\b", "worker"),
    (r"\b(sleepers|sleeping|asleep)\b", "sleep"),
    (r"\b(disappears|disappearing|vanish|vanishing)\b", "disappear"),
)
SETTING_MODIFIERS = set(
    "underground automated empty abandoned closed sleeping sleep quarter "
    "quarters annex building facility complex room level night new old".split()
)


def terms(text: str, *, setting=False) -> set[str]:
    text = normalized(text)
    for pattern, replacement in ALIASES:
        text = re.sub(pattern, replacement, text)
    result = set(text.split()) - STOP
    if setting:
        result -= SETTING_MODIFIERS
    return result


def token_similarity(left: str, right: str, *, setting=False) -> float:
    a, b = terms(left, setting=setting), terms(right, setting=setting)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def palette_key(palette) -> tuple[str, ...]:
    return tuple(sorted({normalized(color) for color in palette}))


def title_parts(title: str):
    words = [w for w in normalized(title).split() if w not in STOP and not w.isdigit()]
    suffix = words[-1] if words else ""
    phrases = tuple(" ".join(words[i : i + 2]) for i in range(len(words) - 1))
    return words, suffix, phrases


def signature(value: WorldConcept | HistoryEntry) -> ConceptSignature:
    if isinstance(value, WorldConcept):
        return value.signature
    return value.signature or ConceptSignature(
        setting=value.specific_setting,
        danger=value.primary_danger or value.danger_type,
        rule=value.survival_rule,
        cost=value.long_term_cost,
        anomaly=value.anomaly,
        inhabitants=value.inhabitants or "no known inhabitants",
    )


def signature_similarity(left: ConceptSignature, right: ConceptSignature):
    scores = {
        field: token_similarity(
            getattr(left, field), getattr(right, field), setting=field == "setting"
        )
        for field in ConceptSignature.model_fields
    }
    weights = dict(setting=0.22, danger=0.17, rule=0.23, cost=0.17, anomaly=0.12, inhabitants=0.09)
    score = sum(scores[field] * weight for field, weight in weights.items())
    # Same venue + routine + residents is a reskin even if an unrelated anomaly differs.
    if scores["setting"] >= 0.72 and scores["rule"] >= 0.72 and scores["inhabitants"] >= 0.72:
        score = max(score, 0.86)
    if scores["setting"] >= 0.72 and scores["danger"] >= 0.75 and scores["rule"] >= 0.75:
        score = max(score, 0.88)
    matched = tuple(field for field, similarity in scores.items() if similarity >= 0.65)
    return round(min(1, score), 3), matched


def assess_candidate(
    candidate: WorldConcept,
    history: tuple[HistoryEntry, ...],
    policy: NoveltyPolicy,
    rating: WorldCandidateRating | None = None,
):
    reasons = []
    exact = history[: policy.recent_exact_history]
    for field, history_field in (
        ("title", "title"),
        ("specific_setting", "specific_setting"),
        ("anomaly", "anomaly"),
        ("critical_rule", "survival_rule"),
        ("long_term_drawback", "long_term_cost"),
    ):
        matches = [
            h
            for h in exact
            if normalized(getattr(candidate, field)) == normalized(getattr(h, history_field))
        ]
        if matches:
            reasons.append(f"exact {field} already used: {matches[0].title}")
    words, suffix, phrases = title_parts(candidate.title)
    titles = history[: policy.title_history_window]
    counts = Counter(w for h in titles for w in set(title_parts(h.title)[0]))
    suffixes = Counter(title_parts(h.title)[1] for h in titles)
    phrase_counts = Counter(p for h in titles for p in title_parts(h.title)[2])
    if suffix and suffixes[suffix] >= policy.title_suffix_limit:
        reasons.append(f'title suffix "{suffix}" reached frequency limit ({suffixes[suffix]})')
    for word in sorted(set(words)):
        if counts[word] >= policy.title_token_limit:
            reasons.append(f'title word "{word}" reached frequency limit ({counts[word]})')
    for phrase in phrases:
        if phrase_counts[phrase] >= policy.title_phrase_limit:
            reasons.append(f'title phrase "{phrase}" already occupied')
    palettes = Counter(palette_key(h.palette) for h in exact)
    if palettes[palette_key(candidate.palette)] >= policy.palette_limit:
        reasons.append("palette combination reached recent frequency limit")
    nearest, similarity, dimensions = None, 0.0, ()
    for entry in history[: policy.recent_semantic_history]:
        score, matched = signature_similarity(candidate.signature, signature(entry))
        if score > similarity:
            nearest, similarity, dimensions = entry, score, matched
    if similarity >= policy.similarity_threshold:
        reasons.append(f"structural similarity {similarity:.2f}: shared " + ", ".join(dimensions))
    novelty = 1 - similarity
    if rating:
        # The scored novelty gate and structural-similarity gate are independent.
        novelty = rating.novelty
        if rating.history_similarity >= policy.similarity_threshold:
            reasons.append(f"critic semantic similarity {rating.history_similarity:.2f}")
        if rating.history_similarity > similarity:
            similarity = rating.history_similarity
            nearest = next((h for h in history if h.fingerprint == rating.nearest_history_id), None)
        for name, value, minimum in (
            ("novelty", rating.novelty, policy.minimum_novelty),
            ("causal coherence", rating.causal_coherence, policy.minimum_coherence),
            ("overall", rating.overall, policy.minimum_overall),
        ):
            if value < minimum:
                reasons.append(f"{name} {value:.2f} below minimum {minimum:.2f}")
        if rating.rejection_reason:
            reasons.append(rating.rejection_reason)
    return CandidateAssessment(
        candidate_id=candidate.candidate_id,
        accepted=not reasons,
        novelty=round(novelty, 3),
        similarity=round(similarity, 3),
        nearest_history_id=nearest.fingerprint if nearest else None,
        nearest_title=nearest.title if nearest else None,
        reasons=tuple(reasons),
    )


def reel_issues(saves, policy: NoveltyPolicy) -> tuple[ReelIssue, ...]:
    issues = []
    chosen = [s for s in saves if isinstance(s.selected, WorldConcept)]
    for a, b in combinations(chosen, 2):
        left, right = a.selected, b.selected
        problems = []
        if normalized(left.title) == normalized(right.title):
            problems.append(("environment", "duplicate titles"))
        for field, dimension in (
            ("primary_danger", "threat"),
            ("danger_type", "threat"),
            ("central_mechanic", "rule"),
            ("critical_rule", "rule"),
            ("long_term_cost_type", "cost"),
            ("long_term_drawback", "cost"),
            ("specific_setting", "environment"),
            ("anomaly", "environment"),
        ):
            if normalized(getattr(left, field)) == normalized(getattr(right, field)):
                problems.append((dimension, f"duplicate {field}"))
        if token_similarity(left.signature.setting, right.signature.setting, setting=True) >= 0.85:
            problems.append(("environment", "effectively the same setting"))
        if token_similarity(left.central_mechanic, right.central_mechanic) >= 0.85:
            problems.append(("rule", "effectively the same central mechanic"))
        similarity, dimensions = signature_similarity(left.signature, right.signature)
        if similarity >= policy.similarity_threshold:
            problems.append(
                ("rule", f"structural similarity {similarity:.2f}: " + ", ".join(dimensions))
            )
        if palette_key(left.palette) == palette_key(right.palette):
            problems.append(("palette", "identical palette combinations"))
        for dimension, reason in dict.fromkeys(problems):
            issues.append(
                ReelIssue(save_ids=(a.save_id, b.save_id), dimension=dimension, reason=reason)
            )
    # The critic also catches partial semantic overlap in these softer dimensions.
    if len(chosen) == 4:
        for field, dimension in (
            ("resource_problem", "resource"),
            ("inhabitants", "inhabitants"),
            ("emotional_mechanic", "emotion"),
        ):
            if len({normalized(getattr(s.selected, field)) for s in chosen}) == 1:
                issues.append(
                    ReelIssue(
                        save_ids=tuple(s.save_id for s in chosen),
                        dimension=dimension,
                        reason=f"all four share {field}",
                    )
                )
    return tuple(issues)


def reel_review_passes(review: ReelDiversityReview, policy: NoveltyPolicy) -> bool:
    dimensions = (
        "environment",
        "resource",
        "threat",
        "rule",
        "inhabitants",
        "cost",
        "emotion",
        "palette",
        "overall",
    )
    return not review.issues and all(
        getattr(review, d) >= policy.minimum_reel_diversity for d in dimensions
    )
