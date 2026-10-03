"""Creative inputs and serializable workflow state, independent of providers."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]
SaveId = Literal["save_01", "save_02", "save_03", "save_04"]
RelativePath = Annotated[str, StringConstraints(min_length=1)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, revalidate_instances="always"
    )


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StageState(Model):
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: Text | None = None


class EnvironmentSpec(Model):
    biome: Text
    setting: Text
    focal_point: Text
    time_of_day: Text
    weather: Text
    lighting: Text
    palette: Annotated[tuple[Text, ...], Field(min_length=1)]
    mood: Text
    details: Annotated[tuple[Text, ...], Field(min_length=1)]
    # Optional so manifests from the initial environment-only workflow still load.
    world_scene: Text | None = None
    shot_composition: Text | None = None
    key_surfaces: tuple[Text, ...] = ()


class CartridgeSpec(Model):
    shell_color: Text
    label_title: Text
    label_symbol: Text
    material: Text
    wear: Text


class EffectsSpec(Model):
    ambient_particles: tuple[Text, ...] = ()
    atmospheric_effects: tuple[Text, ...] = ()
    motion_cues: tuple[Text, ...] = ()


class ConceptRequest(Model):
    theme: Text = "forgotten worlds"


class SaveGameConcept(Model):
    survivability_tier: Literal["best", "good", "risky", "bad"] | None = None
    title: Text
    summary: Text
    environment: EnvironmentSpec
    cartridge: CartridgeSpec
    effects: EffectsSpec


class ReelConcept(Model):
    title: Text
    saves: Annotated[tuple[SaveGameConcept, ...], Field(min_length=4, max_length=4)]


class TemplateReference(Model):
    name: Text
    version: Annotated[str, StringConstraints(pattern=r"^v[1-9][0-9]*$")]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class CompiledPrompt(Model):
    text: Annotated[str, Field(min_length=1)]
    template: TemplateReference


class Artifact(Model):
    path: RelativePath
    media_type: Text
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or path == PurePosixPath("."):
            raise ValueError("artifact paths must be relative to the run directory")
        return value


class SaveGame(SaveGameConcept):
    save_id: SaveId
    environment_prompt: CompiledPrompt | None = None
    label_prompt: CompiledPrompt | None = None
    broll_still_prompt: CompiledPrompt | None = None
    stages: dict[str, StageState] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)


class StoryGeneration(Model):
    """Optional provenance for the separately checkpointed survival-story workflow."""

    provider: Text
    model: Text
    templates: tuple[TemplateReference, ...]
    state: Artifact
    review: Artifact


class Reel(Model):
    schema_version: Literal["1.0"] = "1.0"
    run_id: RunId
    created_at: datetime = Field(default_factory=utc_now)
    request: ConceptRequest
    concept_provider: Text
    title: Text
    saves: Annotated[tuple[SaveGame, ...], Field(min_length=4, max_length=4)]
    status: StageStatus = StageStatus.PENDING
    stages: dict[str, StageState] = Field(default_factory=dict)
    story_generation: StoryGeneration | None = None

    @field_validator("saves")
    @classmethod
    def unique_save_ids(cls, saves: tuple[SaveGame, ...]) -> tuple[SaveGame, ...]:
        if len({save.save_id for save in saves}) != 4:
            raise ValueError("a reel must contain four distinct save IDs")
        return saves
