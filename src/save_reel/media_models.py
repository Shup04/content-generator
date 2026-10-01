"""State for one real B-roll branch, separate from a four-save Reel."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from save_reel.models import (
    Artifact,
    CompiledPrompt,
    Model,
    RunId,
    StageState,
    StageStatus,
    Text,
    utc_now,
)


class BrollValues(Model):
    model_config = ConfigDict(validate_by_name=True)

    game_title: Text = Field(alias="GAME_TITLE")
    world_scene: Text = Field(alias="WORLD_SCENE")
    colour_palette: Text = Field(alias="COLOUR_PALETTE")
    mood: Text = Field(alias="MOOD")
    shot_composition: Text = Field(alias="SHOT_COMPOSITION")
    key_surfaces: Text = Field(alias="KEY_SURFACES")


class MotionValues(Model):
    model_config = ConfigDict(validate_by_name=True)

    camera_motion: Text = Field(alias="CAMERA_MOTION")
    environment_motion: Text = Field(alias="ENVIRONMENT_MOTION")
    audio: Text = Field(alias="AUDIO")


class BrollSettings(Model):
    image_model: Literal["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"] = "gpt-image-2.5-sunburst"
    image_size: Literal["864x1536"] = "864x1536"
    image_quality: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    video_model: Literal["MiniMax-H3"] = "MiniMax-H3"
    duration: Annotated[int, Field(ge=4, le=15)] = 5
    resolution: Literal["768P", "2K"] = "768P"


class BrollRun(Model):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["broll_generation"] = "broll_generation"
    run_id: RunId
    created_at: datetime = Field(default_factory=utc_now)
    values: BrollValues
    motion: MotionValues
    settings: BrollSettings
    still_prompt: CompiledPrompt
    video_prompt: CompiledPrompt
    status: StageStatus = StageStatus.PENDING
    stages: dict[str, StageState] = Field(
        default_factory=lambda: {"image_generation": StageState(), "video_generation": StageState()}
    )
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    image_request_id: str | None = None
    image_usage: dict[str, Any] = Field(default_factory=dict)
    video_task_id: str | None = None
    video_task_status: str | None = None
    video_usage: dict[str, Any] = Field(default_factory=dict)
    # Persisted before each paid POST. An ambiguous request is never retried automatically.
    image_attempted: bool = False
    video_attempted: bool = False
