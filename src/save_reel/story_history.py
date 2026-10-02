"""Compact, content-addressed history and transparent novelty penalties. No embeddings."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from save_reel.storage import RunStore
from save_reel.story_models import (
    HistoryEntry,
    NoveltyPolicy,
    StoryRun,
    SurvivalCandidate,
    WorldConcept,
    WorldSeed,
    normalized,
)

# Penalize these recurring tropes, not every mention of an ordinary object.
OVERUSED = (
    "pool",
    "mall",
    "hotel",
    "train",
    "highway",
    "resort",
    "memory loss",
    "repeating day",
    "disappear",
    "faceless",
    "whisper",
    "endless hallway",
)
CLICHES = (
    "forgotten memories",
    "shadows whisper",
    "whispering shadows",
    "echoes of forgotten",
    "world remembers",
    "everyone forgets you",
    "lose memories",
)
STOP_WORDS = set(
    "a an the you your in on of to and or is are for from with at it that this".split()
)


def tokens(text: str) -> set[str]:
    return set(normalized(text).split()) - STOP_WORDS


def overlap(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    return len(a & b) / max(1, len(a | b))


def candidate_text(candidate: SurvivalCandidate) -> str:
    return " ".join(
        (
            candidate.premise,
            candidate.primary_danger,
            candidate.critical_rule,
            candidate.long_term_drawback,
            candidate.visual_hook,
        )
    )


def novelty_penalty(
    candidate: SurvivalCandidate, seed: WorldSeed, history: tuple[HistoryEntry, ...]
) -> float:
    text = normalized(candidate_text(candidate))
    penalty = sum(5 for phrase in CLICHES if phrase in text)
    if not history:
        return float(penalty)
    nearest = 0.0
    for previous in history:
        similarity = (
            7 * overlap(candidate.critical_rule, previous.survival_rule)
            + 4 * overlap(candidate.visual_hook, previous.visual_hook)
            + 3 * overlap(candidate.long_term_drawback, previous.long_term_cost)
            + 3 * (seed.specific_setting == previous.specific_setting)
            + 1 * (seed.danger_type == previous.danger_type)
        )
        if normalized(candidate.title) == normalized(previous.title):
            similarity += 20
        nearest = max(nearest, similarity)
    for trope in OVERUSED:
        if trope in text:
            frequency = sum(
                trope
                in normalized(
                    " ".join((h.specific_setting, h.visual_hook, h.survival_rule, h.long_term_cost))
                )
                for h in history
            ) / len(history)
            penalty += 12 * frequency
    return round(penalty + nearest, 3)


class StoryHistory:
    def __init__(self, directory: Path):
        self.directory = directory

    def recent(self, count: int) -> tuple[HistoryEntry, ...]:
        if count == 0 or not self.directory.exists():
            return ()
        entries = [
            HistoryEntry.model_validate_json(p.read_text(encoding="utf-8"))
            for p in self.directory.glob("*.json")
        ]
        return tuple(
            sorted(entries, key=lambda e: (e.created_at, e.fingerprint), reverse=True)[:count]
        )

    def record(self, run: StoryRun) -> None:
        for slot in run.saves:
            final, seed = slot.final, slot.seed
            if final is None:
                raise ValueError("cannot record unfinished stories")
            brief = final.brief
            # Narration-only changes and unchanged saves in a fork do not inflate history.
            identity = json.dumps(
                {
                    "seed": seed.model_dump(mode="json"),
                    "brief": brief.model_dump(mode="json", exclude={"narration"}),
                },
                sort_keys=True,
            )
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            path = self.directory / f"{fingerprint}.json"
            old = (
                HistoryEntry.model_validate_json(path.read_text(encoding="utf-8"))
                if path.exists()
                else None
            )
            selected = slot.selected
            authored = isinstance(selected, WorldConcept)
            entry = HistoryEntry(
                fingerprint=fingerprint,
                created_at=old.created_at if old else (run.completed_at or run.created_at),
                run_id=old.run_id if old else run.run_id,
                save_id=old.save_id if old else slot.save_id,
                title=brief.title,
                setting_family=seed.setting_family,
                specific_setting=selected.specific_setting if authored else seed.specific_setting,
                visual_hook=brief.visual_hook,
                danger_type=selected.danger_type,
                survival_rule=brief.survival.critical_rule,
                resource_problem=selected.resource_problem
                if authored
                else seed.main_resource_problem,
                long_term_cost_type=selected.long_term_cost_type,
                long_term_cost=brief.survival.long_term_cost,
                inhabitants=brief.survival.inhabitants,
                anomaly=final.environment.visual_anomaly,
                palette=final.environment.palette,
                emotions=(seed.surface_emotion, seed.deeper_emotion),
                primary_danger=selected.primary_danger,
                central_mechanic=selected.central_mechanic if authored else selected.critical_rule,
                emotional_mechanic=selected.emotional_mechanic if authored else seed.deeper_emotion,
                signature=selected.signature if authored else None,
            )
            if old != entry:
                RunStore._write_atomic(path, (entry.model_dump_json(indent=2) + "\n").encode())

    def import_runs(self, sources: tuple[Path, ...]) -> tuple[int, int]:
        """Merge completed v1/v2 story checkpoints. Never rewrite source runs."""
        from save_reel.models import StageStatus
        from save_reel.story_pipeline import StoryWorkflow

        paths = set()
        for source in sources:
            if not source.exists():
                raise ValueError(f"history source does not exist: {source}")
            if source.is_file():
                if source.name == "manifest.json":
                    manifest = RunStore(source.parent).load_manifest()
                    if manifest.story_generation:
                        paths.add(source.parent / manifest.story_generation.state.path)
                elif source.name == "story.json":
                    paths.add(source)
                else:
                    raise ValueError(
                        "history input must be a run, runs root, or story/manifest.json"
                    )
            elif (source / "story.json").exists():
                paths.add(source / "story.json")
            else:
                paths.update(source.glob("*/story.json"))
        runs = [StoryWorkflow.load(path.parent) for path in sorted(paths)]
        accepted = [run for run in runs if run.status == StageStatus.COMPLETED]
        for run in accepted:
            self.record(run)
        return len(accepted), len(list(self.directory.glob("*.json")))


def creative_history(history: tuple[HistoryEntry, ...], policy: NoveltyPolicy) -> dict:
    """Bounded negative memory: frequency sets and short signatures, never narration."""
    from save_reel.story_novelty import palette_key, signature, title_parts

    exact = history[: policy.recent_exact_history]
    semantic = history[: policy.recent_semantic_history]
    titles = history[: policy.title_history_window]
    limit = policy.summary_set_limit

    def common(values):
        counts = Counter(values)
        return [{"value": value, "count": count} for value, count in counts.most_common(limit)]

    def short(value, words=9):
        return " ".join(normalized(value).split()[:words])

    return {
        "windows": {"exact": len(exact), "semantic": len(semantic), "titles": len(titles)},
        "recent_title_patterns": {
            "titles": [h.title for h in titles],
            "common_words": common(w for h in titles for w in set(title_parts(h.title)[0])),
            "suffixes": common(title_parts(h.title)[1] for h in titles),
            "phrases": common(p for h in titles for p in title_parts(h.title)[2]),
        },
        "setting_families": common(h.setting_family for h in exact),
        "recent_settings": common(short(h.specific_setting) for h in exact),
        "recent_dangers": common(short(h.primary_danger or h.danger_type) for h in exact),
        "recent_rules": common(short(h.survival_rule) for h in exact),
        "recent_costs": common(short(h.long_term_cost) for h in exact),
        "recent_anomalies": common(short(h.anomaly) for h in exact),
        "recent_visual_hooks": common(short(h.visual_hook) for h in exact),
        "resource_problems": common(short(h.resource_problem) for h in exact),
        "inhabitant_concepts": common(short(h.inhabitants or "none known") for h in exact),
        "palettes": common(" / ".join(palette_key(h.palette)) for h in exact),
        "emotional_mechanics": common(
            short(h.emotional_mechanic or " ".join(h.emotions)) for h in exact
        ),
        "recent_save_summaries": [
            {
                "history_id": h.fingerprint,
                "title": h.title,
                "summary": "; ".join(
                    f"{key}: {short(value)}" for key, value in signature(h).model_dump().items()
                ),
            }
            for h in semantic
        ],
    }
