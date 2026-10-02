"""Checkpointed seed -> candidates -> review -> selection -> brief workflow."""

import copy
import hashlib
import json
import secrets
import threading
from contextlib import contextmanager
from itertools import product
from pathlib import Path

from pydantic import ValidationError

from save_reel.concurrency import parallel_each
from save_reel.models import ConceptRequest, StageStatus, utc_now
from save_reel.pipeline import run_logging
from save_reel.providers.story import StoryProvider
from save_reel.storage import RunStore
from save_reel.story_export import export_story, review_text
from save_reel.story_history import (
    StoryHistory,
    candidate_text,
    creative_history,
    novelty_penalty,
    overlap,
)
from save_reel.story_models import (
    SAVE_IDS,
    BroadWorldSeed,
    CandidateReview,
    CandidateSet,
    FinalStory,
    NarrationDraft,
    NarrationStats,
    StoryRun,
    StorySave,
    StorySettings,
    WorldConcept,
    normalized,
)
from save_reel.story_narration_models import TraveloguePolicy, TravelogueState
from save_reel.story_prompting import load_prompts, load_travelogue_prompts, render_request
from save_reel.story_seeds import make_seeds


class StoryWorkflow:
    def __init__(
        self, provider: StoryProvider, *, history: StoryHistory | None = None,
        max_workers: int = 1, progress=None,
    ):
        if not 1 <= max_workers <= 4:
            raise ValueError("Parallel saves must be between 1 and 4")
        self.provider = provider
        self.history = history
        self.max_workers = max_workers
        self.progress = progress or (lambda *args: None)

    def parallel_slots(self, run, store, logger, stage, function):
        """Workers see stable reel context and checkpoint only their own save.

        The coordinator alone performs cross-save selection and history writes.
        A failed worker cannot overwrite another save's successful checkpoint.
        """
        if self.max_workers == 1:
            for slot in run.saves:
                self.progress(stage, slot.save_id, "running")
                try:
                    function(self, run, slot, store, logger)
                except Exception:
                    self.progress(stage, slot.save_id, "failed")
                    raise
                self.progress(stage, slot.save_id, "completed")
            return
        snapshots = [run.model_copy(deep=True) for _ in run.saves]
        lock = threading.RLock()

        def work(index):
            local = snapshots[index]
            slot = local.saves[index]
            worker = copy.copy(self)

            def checkpoint(_run, _store):
                with lock:
                    saves = list(run.saves)
                    saves[index] = slot.model_copy(deep=True)
                    run.saves = tuple(saves)
                    self._save(run, store)

            worker._save = checkpoint
            self.progress(stage, slot.save_id, "running")
            try:
                function(worker, local, slot, store, logger)
                checkpoint(local, store)
            except Exception:
                self.progress(stage, slot.save_id, "failed")
                raise
            self.progress(stage, slot.save_id, "completed")

        parallel_each(work, range(len(run.saves)), self.max_workers)

    @staticmethod
    def load(run_dir: Path) -> StoryRun:
        run = StoryRun.model_validate_json((run_dir / "story.json").read_text(encoding="utf-8"))
        if run.run_id != run_dir.name:
            raise ValueError("story run_id does not match the run directory")
        return run

    def _history(self, runs_dir: Path) -> StoryHistory:
        return self.history or StoryHistory(runs_dir / ".story-history")

    def create(
        self,
        request: ConceptRequest,
        *,
        settings: StorySettings | None = None,
        runs_dir: Path = Path("runs"),
        run_id: str | None = None,
        random_seed: int | None = None,
        prompts_dir: Path | None = None,
    ) -> StoryRun:
        settings = settings or StorySettings()
        templates = load_prompts(settings.prompt_version, prompts_dir)
        travelogue = settings.travelogue if settings.prompt_version == "v3" else None
        if travelogue:
            templates.update(load_travelogue_prompts(travelogue.prompt_version, prompts_dir))
        history = self._history(runs_dir).recent(
            settings.recent_history_count
            if settings.prompt_version == "v1"
            else settings.novelty.history_count
        )
        number = random_seed if random_seed is not None else secrets.randbits(63)
        seeds, reference = make_seeds(number, settings, history)
        store = RunStore.create(runs_dir, run_id)
        run = StoryRun(
            schema_version={"v1": "1.0", "v2": "2.0"}.get(settings.prompt_version, "3.0"),
            run_id=store.run_dir.name,
            request=request,
            provider=self.provider.name,
            model=self.provider.model,
            settings=settings,
            random_seed=number,
            templates=templates,
            seed_catalog=reference,
            history=history,
            saves=tuple(
                StorySave(save_id=sid, seed=seed,
                          travelogue=TravelogueState(policy=travelogue) if travelogue else None)
                for sid, seed in zip(SAVE_IDS, seeds, strict=True)
            ),
        )
        self._save(run, store)
        return self._execute(run, store)

    def resume(self, run_dir: Path) -> StoryRun:
        run = self.load(run_dir)
        return self._execute(run, RunStore(run_dir))

    def regenerate(
        self,
        source: Path,
        *,
        scope: str,
        save_id: str | None = None,
        runs_dir: Path | None = None,
        run_id: str | None = None,
        random_seed: int | None = None,
        narration_style: str | None = None,
    ) -> StoryRun:
        old = self.load(source)
        if narration_style is not None and (
            narration_style not in ("travelogue", "sol")
            or scope != "narration" or old.schema_version != "3.0"
        ):
            raise ValueError(
                "--narration-style requires --scope narration and a v3 world"
            )
        if scope not in ("reel", "save", "candidates", "narration"):
            raise ValueError("unknown regeneration scope")
        if scope != "reel" and save_id not in SAVE_IDS:
            raise ValueError("save, candidates and narration regeneration require --save-id")
        if scope == "reel" and save_id is not None:
            raise ValueError("reel regeneration does not accept --save-id")
        if old.provider != self.provider.name or old.model != self.provider.model:
            raise ValueError("regeneration must use the saved provider and model")
        root = runs_dir or source.parent
        history = self._history(root).recent(
            old.settings.recent_history_count
            if old.schema_version == "1.0"
            else old.settings.novelty.history_count
        )
        number = random_seed if random_seed is not None else secrets.randbits(63)
        fixed = (
            ()
            if scope == "reel"
            else tuple(s.seed for s in old.saves if scope != "save" or s.save_id != save_id)
        )
        seeds, reference = make_seeds(number, old.settings, history, fixed=fixed)
        saves = []
        travelogue = old.settings.travelogue if old.schema_version == "3.0" else None
        for slot, seed in zip(old.saves, seeds, strict=True):
            if scope == "reel" or (scope == "save" and slot.save_id == save_id):
                saves.append(StorySave(save_id=slot.save_id, seed=seed,
                                      travelogue=TravelogueState(policy=travelogue)
                                      if travelogue else None))
            elif scope == "candidates" and slot.save_id == save_id:
                saves.append(StorySave(save_id=slot.save_id, seed=slot.seed,
                                      travelogue=TravelogueState(policy=travelogue)
                                      if travelogue else None))
            else:
                saves.append(slot.model_copy(deep=True))
        if scope == "narration" and not next(s for s in saves if s.save_id == save_id).final:
            raise ValueError("narration regeneration requires an existing final brief")
        templates = dict(old.templates)
        if scope == "narration" and old.schema_version == "3.0":
            slot = next(s for s in saves if s.save_id == save_id)
            slot.final = None
            slot.narration_stats = None
            slot.narration_draft = None
            slot.grounding_review = None
            slot.narration_attempts = ()
            if narration_style in ("travelogue", "sol") or slot.travelogue:
                policy = slot.travelogue.policy if slot.travelogue else (
                    old.settings.travelogue or TraveloguePolicy()
                )
                if narration_style == "sol":
                    policy = TraveloguePolicy.sol()
                slot.travelogue = TravelogueState(policy=policy)
                if narration_style is not None:
                    templates.update(load_travelogue_prompts(policy.prompt_version))
        store = RunStore.create(root, run_id)
        run = StoryRun(
            schema_version=old.schema_version,
            run_id=store.run_dir.name,
            source_run_id=old.run_id,
            request=old.request,
            provider=old.provider,
            model=old.model,
            settings=old.settings,
            random_seed=number,
            templates=templates,
            seed_catalog=reference,
            history=history,
            saves=tuple(saves),
            regeneration=scope,
            regeneration_save_id=save_id,
            narration_pending=scope == "narration" and old.schema_version != "3.0",
            frozen_save_ids=tuple(
                s.save_id
                for s in saves
                if s.final and (scope == "narration" or s.save_id != save_id)
            ),
            reel_review=old.reel_review if scope == "narration" else None,
            reel_review_history=old.reel_review_history if scope == "narration" else (),
        )
        self._save(run, store)
        return self._execute(run, store)

    @staticmethod
    def _save(run, store):
        validated = StoryRun.model_validate(run)
        store.write_text(
            "story.json", validated.model_dump_json(indent=2) + "\n", "application/json"
        )

    @staticmethod
    @contextmanager
    def _lock(store):
        lock = store.run_dir / ".story.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError(
                "story run is locked; if a process was killed, confirm it has "
                "stopped before removing .story.lock"
            ) from None
        try:
            yield
        finally:
            lock.rmdir()

    def _context(self, run, slot):
        settings = run.settings.model_dump(
            mode="json",
            exclude={"travelogue", "narration_model"} | (
                {"world_reasoning_effort", "narration_reasoning_effort"}
                if run.schema_version != "3.0" else set()
            ),
        )
        if run.schema_version != "1.0":
            context = {
                "theme": run.request.theme,
                "settings": settings,
                "narration_policy": run.settings.narration.model_dump(mode="json"),
            }
            if slot is not None:
                context.update(
                    save_id=slot.save_id,
                    seed=slot.seed.model_dump(mode="json"),
                    candidate_round=slot.candidate_round,
                    replacement_feedback=list(slot.replacement_feedback),
                    creative_history=creative_history(run.history, run.settings.novelty),
                    other_saves=[
                        {
                            "save_id": s.save_id,
                            "seed": s.seed.model_dump(mode="json"),
                            "selected": {
                                "signature": s.selected.signature.model_dump(mode="json"),
                                "danger_type": s.selected.danger_type,
                                "central_mechanic": s.selected.central_mechanic,
                                "long_term_cost_type": s.selected.long_term_cost_type,
                                "resource_problem": s.selected.resource_problem,
                                "emotional_mechanic": s.selected.emotional_mechanic,
                                "palette": list(s.selected.palette),
                            }
                            if s.selected
                            else None,
                        }
                        for s in run.saves
                        if s.save_id != slot.save_id
                    ],
                )
            return context
        return {
            "theme": run.request.theme,
            "save_id": slot.save_id,
            "seed": slot.seed.model_dump(mode="json"),
            "settings": settings,
            "narration_policy": run.settings.narration.model_dump(mode="json"),
            "recent_history": [h.model_dump(mode="json") for h in run.history],
            "other_saves": [
                dict(
                    save_id=s.save_id,
                    seed=s.seed.model_dump(mode="json"),
                    selected=s.selected.model_dump(mode="json") if s.selected else None,
                )
                for s in run.saves
                if s.save_id != slot.save_id
            ],
        }

    def _generate(self, run, slot, store, logger, stage, response_type, check, **extra):
        context = {**self._context(run, slot), **extra}
        if run.schema_version == "3.0" and stage in ("world_simulation", "world_review"):
            context = {
                "save_id": slot.save_id,
                "selected": slot.selected.model_dump(mode="json"),
                "minimum_coherence": run.settings.novelty.minimum_coherence,
                **extra,
            }
        if run.schema_version == "3.0" and stage.startswith("narration"):
            context = {
                "save_id": slot.save_id,
                "title": slot.selected.title,
                "approved_facts": slot.world.spec.facts(),
                "narration_policy": slot.spoken_policy(run.settings).model_dump(mode="json"),
                **extra,
            }
            if stage == "narration_description":
                context = {"title": slot.selected.title, "approved_facts": slot.world.spec.facts()}
            elif stage in (
                "narration_prose_candidates", "narration_prose_candidate",
                "narration_prose_selection",
            ):
                policy = slot.travelogue.policy
                context = {
                    "title": slot.selected.title,
                    "world_description": slot.travelogue.description.description,
                    "word_range": [policy.target_min_words, policy.target_max_words],
                    **extra,
                }
        if stage in ("brief", "narration"):
            context.pop("creative_history", None)  # Preserve the selected story; no re-invention.
        address = slot.save_id if slot else "reel"
        model = run.settings.model_for(stage) if run.provider == "openai" else run.model
        feedback = ""
        directory = store.run_dir / "story_requests" / address / stage
        # Recover a response persisted immediately before an interruption, even if its
        # StoryRun checkpoint had not yet been written. Never reuse a different input.
        base_prompt = render_request(run.templates[stage], context)
        for cached_path in sorted(directory.glob("*.json"), reverse=True):
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            if (
                cached.get("status") == "completed"
                and cached.get("context") == context
                and cached.get("base_prompt") == base_prompt
                and cached.get("provider") == run.provider
                and cached.get("model") == model
                and cached.get("reasoning_effort") == run.settings.effort_for(stage)
            ):
                result = response_type.model_validate(cached["response"])
                check(result)
                logger.info("Reusing persisted %s %s response", address, stage)
                return result
        previous_attempts = len(list(directory.glob("*.json"))) if directory.exists() else 0
        for attempt in range(run.settings.validation_retries + 1):
            prompt = render_request(run.templates[stage], context, feedback)
            request_path = (
                f"story_requests/{address}/{stage}/"
                f"attempt_{previous_attempts + attempt + 1:03d}.json"
            )
            record = dict(
                stage=stage,
                provider=run.provider,
                model=model,
                created_at=utc_now().isoformat(),
                prompt=prompt,
                base_prompt=base_prompt,
                context=context,
                template=run.templates[stage].template.model_dump(),
                parameters=run.settings.model_dump(mode="json"),
                reasoning_effort=run.settings.effort_for(stage),
                status="requested",
            )
            store.write_text(request_path, json.dumps(record, indent=2), "application/json")
            try:
                value = self.provider.generate(
                    stage=stage, prompt=prompt, context=context, response_type=response_type
                )
                result = response_type.model_validate(value)
                check(result)
            except (ValidationError, ValueError) as exc:
                feedback = (
                    str(exc)
                    if not isinstance(exc, ValidationError)
                    else "; ".join(
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                        for e in exc.errors(include_input=False, include_url=False)
                    )
                )
                logger.warning(
                    "%s %s validation attempt %d failed: %s",
                    address,
                    stage,
                    attempt + 1,
                    feedback,
                )
                record.update(status="invalid", error=feedback)
                store.write_text(request_path, json.dumps(record, indent=2), "application/json")
                if attempt == run.settings.validation_retries:
                    raise ValueError(
                        f"{address} {stage} validation exhausted: {feedback}"
                    ) from None
            else:
                record.update(status="completed", response=result.model_dump(mode="json"))
                store.write_text(request_path, json.dumps(record, indent=2), "application/json")
                logger.info("%s %s completed", address, stage)
                return result
        raise AssertionError("unreachable")

    @staticmethod
    def _check_candidates(run, slot, result):
        count = run.settings.candidates_per_save
        if len(result.candidates) != count:
            raise ValueError(f"expected {count} candidates")
        expected = {f"{slot.save_id}_candidate_{i}" for i in range(1, count + 1)}
        if {c.candidate_id for c in result.candidates} != expected:
            raise ValueError(f"candidate IDs must be {sorted(expected)}")
        for candidate in result.candidates:
            if isinstance(slot.seed, BroadWorldSeed):
                if not isinstance(candidate, WorldConcept):
                    raise ValueError("v2 requires complete authored world concepts")
                if len(set(map(normalized, candidate.palette))) != len(candidate.palette):
                    raise ValueError("palette colors must be distinct")
                continue
            if (
                candidate.danger_type != slot.seed.danger_type
                or candidate.long_term_cost_type != slot.seed.long_term_cost_type
            ):
                raise ValueError("candidate danger_type and long_term_cost_type must match seed")

    @staticmethod
    def _check_review(slot, result):
        expected = {c.candidate_id for c in slot.candidates.candidates}
        ids = [r.candidate_id for r in result.ratings]
        if len(ids) != len(expected) or set(ids) != expected:
            raise ValueError("review needs exactly one rating for every candidate ID")

    @staticmethod
    def _check_world_review(run, slot, result):
        StoryWorkflow._check_review(slot, result)
        known = {h.fingerprint for h in run.history[: run.settings.novelty.recent_semantic_history]}
        for rating in result.ratings:
            if rating.nearest_history_id is not None and rating.nearest_history_id not in known:
                raise ValueError(
                    "critic nearest_history_id must identify supplied history; "
                    f"invalid ID: {rating.nearest_history_id[:80]!r}. "
                    "Copy an exact history_id from creative_history.recent_save_summaries. "
                    "Do not use a title or current-reel save_id. If no prior save is "
                    "meaningfully similar, use null and history_similarity=0; explain "
                    "current-reel comparisons in rationale."
                )
            if rating.nearest_history_id is None and rating.history_similarity != 0:
                raise ValueError("nonzero history similarity requires a nearest history ID")

    @staticmethod
    def _check_final(run, slot, result):
        if result.brief.title != slot.selected.title:
            raise ValueError("final title must match the selected candidate")
        # The generator may elaborate optional fields, but cannot rewrite the central bargain.
        for field, expected in (
            ("main_threat", slot.selected.primary_danger),
            ("critical_rule", slot.selected.critical_rule),
            ("rule_consequence", slot.selected.rule_consequence),
            ("long_term_cost", slot.selected.long_term_drawback),
        ):
            if getattr(result.brief.survival, field) != expected:
                raise ValueError(f"survival.{field} must preserve the selected text exactly")
        run.settings.narration.check(result.brief.narration)
        if isinstance(slot.selected, WorldConcept):
            if (
                result.environment.palette != slot.selected.palette
                or result.environment.visual_anomaly != slot.selected.anomaly
            ):
                raise ValueError("final environment must preserve selected palette and anomaly")
            if result.environment.time_of_day != slot.selected.time_of_day:
                raise ValueError("final time_of_day must match the selected world")
            if (
                result.brief.survival.inhabitants != slot.selected.inhabitants
                or result.brief.survival.shelter != slot.selected.safe_sleeping
                or result.brief.visual_hook != slot.selected.visual_hook
            ):
                raise ValueError("final brief must preserve inhabitants, shelter and visual hook")

    @staticmethod
    def _select(run):
        options = []
        for slot in run.saves:
            if slot.selected:
                options.append([(slot.selected, slot.selection_score or 0.0)])
                continue
            ratings = {r.candidate_id: r for r in slot.review.ratings}
            eligible = [
                (c, ratings[c.candidate_id].total - novelty_penalty(c, slot.seed, run.history))
                for c in slot.candidates.candidates
                if not ratings[c.candidate_id].reject
            ]
            if not eligible:
                raise ValueError(f"{slot.save_id} has no eligible candidates")
            options.append(eligible)
        best, best_score = None, float("-inf")
        for combination in product(*options):
            candidates = [c for c, _ in combination]
            if len({normalized(c.title) for c in candidates}) != 4:
                continue
            score = sum(s for _, s in combination)
            score -= sum(
                4 * overlap(candidate_text(a), candidate_text(b))
                for i, a in enumerate(candidates)
                for b in candidates[i + 1 :]
            )
            if score > best_score:
                best, best_score = combination, score
        if best is None:
            raise ValueError("no four distinct eligible titles; regenerate candidate sets")
        for slot, (candidate, score) in zip(run.saves, best, strict=True):
            if slot.selected is None:
                slot.selected = candidate
                slot.selection_score = round(score, 3)

    def _execute(self, run, store):
        with self._lock(store), run_logging(store) as logger:
            logger.info("Story run: %s (%s / %s)", store.run_dir, run.provider, run.model)
            if run.status == StageStatus.COMPLETED and store.manifest_path.exists():
                manifest = store.load_manifest()
                provenance = manifest.story_generation
                if provenance:
                    digest = hashlib.sha256((store.run_dir / "story.json").read_bytes()).hexdigest()
                    if digest != provenance.state.sha256:
                        raise ValueError("story cache checksum does not match the manifest")
                    self._history(store.run_dir.parent).record(run)
                    logger.info("Reusing completed story run; no provider calls")
                    return run
            needs_generation = run.narration_pending or any(s.final is None for s in run.saves)
            if needs_generation and (run.provider, run.model) != (
                self.provider.name,
                self.provider.model,
            ):
                raise ValueError("resume requires the saved provider and model")
            run.status = StageStatus.RUNNING
            run.error = None
            self._save(run, store)
            try:
                if run.schema_version != "1.0":
                    from save_reel.story_development import develop_worlds

                    develop_worlds(self, run, store, logger)
                else:
                    for slot in run.saves:
                        if slot.candidates is None:
                            slot.candidates = self._generate(
                                run,
                                slot,
                                store,
                                logger,
                                "candidates",
                                CandidateSet,
                                lambda r, s=slot: self._check_candidates(run, s, r),
                            )
                            self._save(run, store)
                    for slot in run.saves:
                        if slot.review is None:
                            slot.review = self._generate(
                                run,
                                slot,
                                store,
                                logger,
                                "review",
                                CandidateReview,
                                lambda r, s=slot: self._check_review(s, r),
                                candidates=slot.candidates.model_dump(mode="json"),
                            )
                            self._save(run, store)
                    self._select(run)
                    self._save(run, store)
                if run.schema_version == "3.0":
                    from save_reel.story_simulation import finish_worlds

                    finish_worlds(self, run, store, logger)
                for slot in run.saves:
                    if slot.final is None:
                        slot.final = self._generate(
                            run,
                            slot,
                            store,
                            logger,
                            "brief",
                            FinalStory,
                            lambda r, s=slot: self._check_final(run, s, r),
                            selected=slot.selected.model_dump(mode="json"),
                        )
                        slot.narration_stats = NarrationStats.measure(
                            slot.final.brief.narration, run.settings.narration
                        )
                        self._save(run, store)
                if run.narration_pending:
                    slot = next(s for s in run.saves if s.save_id == run.regeneration_save_id)
                    draft = self._generate(
                        run,
                        slot,
                        store,
                        logger,
                        "narration",
                        NarrationDraft,
                        lambda r: run.settings.narration.check(r.narration),
                        brief=slot.final.brief.model_dump(mode="json"),
                    )
                    slot.final.brief.narration = draft.narration
                    slot.narration_stats = NarrationStats.measure(
                        draft.narration, run.settings.narration
                    )
                    run.narration_pending = False
                    self._save(run, store)
                run.completed_at = utc_now()
                run.status = StageStatus.COMPLETED
                self._save(run, store)
                export_story(run, store)
                self._history(store.run_dir.parent).record(run)
                logger.info("Story review ready: %s", store.run_dir / "story_review.txt")
                return run
            except Exception as exc:
                run.status = StageStatus.FAILED
                run.error = str(exc) or type(exc).__name__
                self._save(run, store)
                store.write_text("story_review.txt", review_text(run))
                logger.error(
                    "Story generation failed in %s: %s. Resume or explicitly regenerate.",
                    store.run_dir,
                    run.error,
                )
                raise
