"""State for real B-roll and cartridge branches, separate from a four-save Reel."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

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


class ImageSettings(Model):
    image_model: Literal["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"] = "gpt-image-2.5-sunburst"
    image_size: Literal["1536x1024", "864x1536"] = "1536x1024"
    image_quality: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Omitted in older saved runs, which were generated with opaque backgrounds.
    image_background: Literal["opaque", "transparent"] = "opaque"


class BrollSettings(ImageSettings):
    image_size: Literal["864x1536"] = "864x1536"
    image_background: Literal["opaque"] = "opaque"
    video_model: Literal["MiniMax-H3", "MiniMax-H3-Max"] = "MiniMax-H3"
    duration: Annotated[int, Field(ge=4, le=15)] = 5
    resolution: Literal["480P", "768P", "2K"] = "768P"

    @model_validator(mode="after")
    def video_capabilities(self):
        if self.video_model == "MiniMax-H3-Max":
            if self.duration < 5 or self.resolution == "2K":
                raise ValueError("MiniMax-H3-Max supports 5–15 seconds at 480P or 768P")
        elif self.resolution == "480P":
            raise ValueError("MiniMax-H3 supports 768P or 2K")
        return self


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
    reused_still_run_id: RunId | None = None
    video_task_id: str | None = None
    video_task_status: str | None = None
    video_usage: dict[str, Any] = Field(default_factory=dict)
    # Persisted before each paid POST. An ambiguous request is never retried automatically.
    image_attempted: bool = False
    video_attempted: bool = False


class CartridgeValues(Model):
    model_config = ConfigDict(validate_by_name=True)

    title: Text = Field(alias="TITLE")
    shell_material_color: Text = Field(alias="SHELL_MATERIAL_COLOR")
    shape_language: Text = Field(alias="SHAPE_LANGUAGE")
    molded_details: Text = Field(alias="MOTIFS_GROOVES_SURFACE_DETAILS")
    object_mood: Text = Field(alias="OBJECT_MOOD")
    label_scene_description: Text | None = Field(default=None, alias="LABEL_SCENE_DESCRIPTION")
    hint_scene_description: Text | None = Field(default=None, alias="HINT_SCENE_DESCRIPTION")

    @model_validator(mode="after")
    def require_one_scene_description(self) -> "CartridgeValues":
        if (self.label_scene_description is None) == (self.hint_scene_description is None):
            raise ValueError(
                "Provide exactly one of LABEL_SCENE_DESCRIPTION or HINT_SCENE_DESCRIPTION"
            )
        return self


class CartridgeRun(Model):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["cartridge_generation"] = "cartridge_generation"
    run_id: RunId
    created_at: datetime = Field(default_factory=utc_now)
    values: CartridgeValues
    settings: ImageSettings
    prompt: CompiledPrompt
    status: StageStatus = StageStatus.PENDING
    stage: StageState = Field(default_factory=StageState)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    image_request_id: str | None = None
    image_usage: dict[str, Any] = Field(default_factory=dict)
    image_attempted: bool = False
