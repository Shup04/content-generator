"""Bounded v2 concept development and reel-level replacement. No media generation."""

from save_reel.story_models import (
    CandidateAttempt,
    ReelDiversityReview,
    ReelReviewAttempt,
    WorldCandidateReview,
    WorldCandidateSet,
    WorldReplacement,
)
from save_reel.story_novelty import assess_candidate, reel_issues, reel_review_passes


def _score(slot, candidate):
    rating = next(r for r in slot.review.ratings if r.candidate_id == candidate.candidate_id)
    assessment = slot.assessments[candidate.candidate_id]
    return (rating.overall + rating.causal_coherence + assessment.novelty) / 3


def _eligible(slot):
    return [
        c
        for c in slot.candidates.candidates
        if slot.assessments[c.candidate_id].accepted
        and c.candidate_id not in slot.excluded_candidates
    ]


def protected_save_ids(run):
    """Approved worlds stay fixed while a failed sibling finds a new concept."""
    return tuple(
        s.save_id for s in run.saves
        if s.save_id in run.frozen_save_ids or s.final or (
            s.world and s.world_review and s.world_review.approved(run.settings.novelty)
        )
    )


def _prepare_slot(workflow, run, slot, store, logger):
    if slot.save_id in protected_save_ids(run):
        return
    while True:
        if slot.candidates is None:
            slot.candidates = workflow._generate(
                run,
                slot,
                store,
                logger,
                "candidates",
                WorldCandidateSet,
                lambda result: workflow._check_candidates(run, slot, result),
            )
            slot.assessments = {
                c.candidate_id: assess_candidate(c, run.history, run.settings.novelty)
                for c in slot.candidates.candidates
            }
            workflow._save(run, store)
        if slot.review is None:
            slot.review = workflow._generate(
                run,
                slot,
                store,
                logger,
                "review",
                WorldCandidateReview,
                lambda result: workflow._check_world_review(run, slot, result),
                candidates=slot.candidates.model_dump(mode="json"),
                hard_checks={k: a.model_dump(mode="json") for k, a in slot.assessments.items()},
            )
            for candidate, rating in (
                (c, next(r for r in slot.review.ratings if r.candidate_id == c.candidate_id))
                for c in slot.candidates.candidates
            ):
                slot.assessments[candidate.candidate_id] = assess_candidate(
                    candidate, run.history, run.settings.novelty, rating
                )
            workflow._save(run, store)
        if _eligible(slot):
            return
        reason = "; ".join(
            dict.fromkeys(reason for a in slot.assessments.values() for reason in a.reasons)
        )
        if slot.candidate_round >= run.settings.novelty.max_candidate_rounds:
            raise ValueError(
                f"{slot.save_id}: no acceptable concepts after "
                f"{slot.candidate_round} rounds. {reason}"
            )
        logger.info("%s replacing rejected candidate set: %s", slot.save_id, reason)
        slot.previous_attempts += (
            CandidateAttempt(
                round_number=slot.candidate_round,
                candidates=slot.candidates,
                review=slot.review,
                assessments=slot.assessments,
                reason=reason or "set exhausted",
            ),
        )
        slot.candidate_round += 1
        slot.replacement_feedback = tuple(
            dict.fromkeys(
                [*slot.replacement_feedback, reason or "Invent a different survival system."]
            )
        )
        slot.selected = None
        slot.candidates = None
        slot.review = None
        slot.assessments = {}
        slot.excluded_candidates = ()
        workflow._save(run, store)


def _replace(run, save_id, reason, logger, *, source="reel diversity"):
    if run.reel_replacements >= run.settings.novelty.max_reel_replacements:
        raise ValueError(f"reel diversity replacement limit reached: {reason}")
    slot = next(s for s in run.saves if s.save_id == save_id)
    if slot.save_id in protected_save_ids(run):
        raise ValueError("cannot replace a frozen save or approved world")
    candidate_id = slot.selected.candidate_id
    slot.excluded_candidates += (candidate_id,)
    assessment = slot.assessments[candidate_id]
    slot.assessments[candidate_id] = assessment.model_copy(
        update={"accepted": False, "reasons": (*assessment.reasons, f"{source}: {reason}")}
    )
    slot.replacement_feedback += (f"Replace this rejected system ({source}): {reason}",)
    if slot.world_attempts or slot.world:
        slot.world_replacements += (WorldReplacement(
            concept=slot.selected, candidate_round=slot.candidate_round,
            attempts=slot.world_attempts, pending_simulation=slot.world, reason=reason,
        ),)
        slot.world = None
        slot.world_review = None
        slot.world_attempts = ()
    slot.selected = None
    slot.selection_score = None
    run.reel_review = None
    run.reel_replacements += 1
    logger.info("Replacing %s: %s", save_id, reason)


def develop_worlds(workflow, run, store, logger):
    if (
        all(s.final for s in run.saves)
        and run.reel_review
        and reel_review_passes(run.reel_review, run.settings.novelty)
    ):
        return
    while True:
        workflow.parallel_slots(run, store, logger, "concepts", _prepare_slot)
        for slot in run.saves:
            if slot.selected is None:
                slot.selected = max(_eligible(slot), key=lambda c: _score(slot, c))
                slot.selection_score = round(_score(slot, slot.selected), 3)
        issues = reel_issues(run.saves, run.settings.novelty)
        if issues:
            involved = {sid for issue in issues for sid in issue.save_ids}
            mutable = [
                s
                for s in run.saves
                if s.save_id in involved and s.save_id not in protected_save_ids(run)
            ]
            if not mutable:
                raise ValueError("frozen saves conflict; regenerate the whole reel")
            weakest = min(mutable, key=lambda s: s.selection_score)
            reason = "; ".join(
                dict.fromkeys(i.reason for i in issues if weakest.save_id in i.save_ids)
            )
            _replace(run, weakest.save_id, reason, logger)
            workflow._save(run, store)
            continue
        workflow._save(run, store)
        if run.reel_review is None:
            workflow.progress("diversity", "reel", "running")
            run.reel_review = workflow._generate(
                run,
                None,
                store,
                logger,
                "reel_review",
                ReelDiversityReview,
                lambda result: _check_reel_review(run, result),
                selected=[
                    {
                        "save_id": s.save_id,
                        "role": s.seed.role.value,
                        "concept": s.selected.model_dump(mode="json"),
                        "selection_score": s.selection_score,
                    }
                    for s in run.saves
                ],
                frozen_save_ids=list(protected_save_ids(run)),
            )
            run.reel_review_history += (
                ReelReviewAttempt(
                    selection={
                        s.save_id: f"{s.candidate_round}:{s.selected.candidate_id}"
                        for s in run.saves
                    },
                    review=run.reel_review,
                ),
            )
            workflow._save(run, store)
        if reel_review_passes(run.reel_review, run.settings.novelty):
            workflow.progress("diversity", "reel", "completed")
            return
        mutable = [s for s in run.saves if s.save_id not in protected_save_ids(run)]
        if not mutable:
            raise ValueError("approved worlds conflict; regenerate the whole reel")
        suggested = next((s for s in mutable if s.save_id == run.reel_review.weakest_save_id), None)
        weakest = suggested or min(mutable, key=lambda s: s.selection_score)
        reason = "; ".join(i.reason for i in run.reel_review.issues) or run.reel_review.rationale
        _replace(run, weakest.save_id, reason, logger)
        workflow._save(run, store)


def _check_reel_review(run, review):
    if review.weakest_save_id in protected_save_ids(run):
        raise ValueError("reel review may only recommend replacing a mutable save")
    for issue in review.issues:
        if len(set(issue.save_ids)) != len(issue.save_ids):
            raise ValueError("reel issues must name distinct save IDs")
