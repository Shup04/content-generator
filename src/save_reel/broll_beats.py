"""Structured shot choices shared by story output and independent media branches."""

import re
from itertools import combinations
from typing import Literal

from save_reel.models import Model, RunId, Text


class BrollBeat(Model):
    type: Literal["establishing", "detail"]
    description: Text
    shot_composition: Text
    camera_motion: Text


class StudioBrollBeat(BrollBeat):
    run_id: RunId | None = None


def validate_beats(beats):
    if not beats:
        return  # Legacy single-shot worlds have no beat array.
    if not 2 <= len(beats) <= 4 or tuple(b.type for b in beats) != (
        "establishing", *("detail" for _ in beats[1:])
    ):
        raise ValueError("B-roll requires an establishing beat followed by 1–3 detail beats")
    for field in ("description", "shot_composition"):
        tokens = [set(re.findall(r"\w+", getattr(b, field).casefold())) for b in beats]
        if any(len(a & b) / max(1, len(a | b)) >= 0.9 for a, b in combinations(tokens, 2)):
            raise ValueError(f"B-roll beats need distinct {field} values, not the same shot")


def beat_values(environment, motion, beat):
    """Retain shared world/style variables; change the visible area and camera only."""
    if beat is None:
        return environment, motion
    return (
        environment.model_copy(update={
            "world_scene": beat.description if beat.description == environment.world_scene else (
                beat.description + "\n\nWorld context: " + environment.world_scene
            ),
            "shot_composition": beat.shot_composition,
        }),
        motion.model_copy(update={"camera_motion": beat.camera_motion}),
    )
