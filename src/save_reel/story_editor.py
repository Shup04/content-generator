"""One cached clarity edit after narration selection. Never changes creative facts."""

import hashlib

from save_reel.models import Model, Text
from save_reel.story_prose_models import ProseParagraph


class ScriptEdit(Model):
    source_narration: tuple[Text, ...]
    concept_sha256: Text
    narration: Text


def source_narration(slot):
    if slot.travelogue and slot.travelogue.selected:
        return (slot.travelogue.selected.paragraph,)
    if slot.narration_draft and slot.grounding_review and not (
        slot.grounding_review.problems(slot.narration_draft)
    ):
        return slot.narration_draft.narration
    raise ValueError("Script editing requires selected narration or a passing grounding review")


def concept_digest(slot):
    return hashlib.sha256(slot.selected.model_dump_json().encode()).hexdigest()


def maximum_words(source, policy):
    from save_reel.story_models import word_count

    return max(word_count(source), policy.target_max_words)


def check_text(text, source, policy):
    from save_reel.story_models import word_count

    if not text.strip() or any(c in text for c in "\n\r"):
        raise ValueError("Edited narration must be one nonempty paragraph")
    # Deletion is allowed; never pad a clarity edit to satisfy the writer's minimum.
    words, limit = word_count((text,)), maximum_words(source, policy)
    if words > limit:
        raise ValueError(
            f"Edited narration has {words} words; the maximum is {limit} "
            f"(original: {word_count(source)} words). Shorten the edit by at least "
            f"{words - limit} words. Remove unnecessary details; do not add new information. "
            "There is no minimum word count."
        )


def validate_edit(run, slot):
    if slot.script_edit:
        if "narration_editor" not in run.templates:
            raise ValueError("A script edit requires its saved editor prompt")
        edit = slot.script_edit
        original = source_narration(slot)
        if edit.source_narration != original or edit.concept_sha256 != concept_digest(slot):
            raise ValueError("Script edit no longer matches the selected world and narration")
        check_text(edit.narration, original, slot.spoken_policy(run.settings))
    elif (slot.final and run.settings.script_editor_prompt_version
          and "narration_editor" in run.templates and slot.save_id not in run.frozen_save_ids):
        raise ValueError("Final narration requires the configured script editor pass")


def edit_script(workflow, run, slot, store, logger):
    if not run.settings.script_editor_prompt_version or slot.script_edit is not None:
        return
    if "narration_editor" not in run.templates:
        raise ValueError("Script editing requires the saved editor prompt")
    original = source_narration(slot)
    result = workflow._generate(
        run, slot, store, logger, "narration_editor", ProseParagraph,
        lambda value: check_text(value.paragraph, original, slot.spoken_policy(run.settings)),
    )
    slot.script_edit = ScriptEdit(
        source_narration=original, concept_sha256=concept_digest(slot), narration=result.paragraph,
    )
    workflow._save(run, store)
