"""Editable Studio drafts. Generated run manifests remain immutable inputs."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.broll_beats import StudioBrollBeat, beat_values, validate_beats
from save_reel.media_models import (
    BrollSettings,
    BrollValues,
    CartridgeValues,
    ImageSettings,
    MotionValues,
)
from save_reel.models import Model, RunId, Text
from save_reel.narration_models import SpeechSettings
from save_reel.polish_models import ReelPolish
from save_reel.render_models import RenderSettings
from save_reel.story_models import StorySettings
from save_reel.story_tiers import SurvivabilityTier

Action = Literal[
    "stories", "scripts", "cartridges", "stills", "videos", "render", "narrate", "full"
]


def narration_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()


class NarrationOrigin(Model):
    provider: Text
    model: Text | None = None
    source_run: RunId | None = None
    text_sha256: Text


class StudioGame(Model):
    title: Text
    narration: Annotated[Text, Field(max_length=1000)]
    cartridge: CartridgeValues
    environment: BrollValues
    motion: MotionValues
    cartridge_run: RunId | None = None
    broll_run: RunId | None = None
    broll_beats: tuple[StudioBrollBeat, ...] = ()
    narration_origin: NarrationOrigin | None = None
    survivability_tier: SurvivabilityTier | None = None

    @property
    def needs_script(self) -> bool:
        return bool(self.narration_origin and self.narration_origin.provider == "mock")

    @model_validator(mode="after")
    def matching_titles(self):
        validate_beats(self.broll_beats)
        if self.title != self.cartridge.title or self.title != self.environment.game_title:
            raise ValueError("Game, cartridge and environment titles must match")
        return self

    def clip_numbers(self, limit=2):
        return range(1, min(limit, len(self.broll_beats) or 1) + 1)

    def clip_values(self, number=1):
        if number not in self.clip_numbers():
            raise ValueError(f"{self.title}: B-roll {number} has no saved beat description")
        beat = self.broll_beats[number - 1] if self.broll_beats else None
        return beat_values(self.environment, self.motion, beat)

    def clip_run(self, number=1):
        if self.broll_beats:
            return self.broll_beats[number - 1].run_id
        return self.broll_run if number == 1 else None

    def set_clip_run(self, number, run_id):
        if self.broll_beats:
            self.broll_beats[number - 1].run_id = run_id
        if number == 1:
            self.broll_run = run_id  # Retain the legacy first-clip pointer for older consumers.


class StudioDraft(Model):
    draft_id: RunId
    revision: int = Field(default=0, ge=0)
    name: Text = "Untitled reel"
    theme: Text = "strange worlds you must survive inside permanently"
    provider: Literal["mock", "openai"] = "mock"
    seed: int | None = None
    parallel_saves: int = Field(default=4, ge=1, le=4)
    speech_parallel_saves: int = Field(default=2, ge=1, le=4)
    preset_revision: RunId | None = None
    story: StorySettings = Field(default_factory=StorySettings)
    cartridge: ImageSettings = Field(
        default_factory=lambda: ImageSettings(image_background="transparent")
    )
    broll: BrollSettings = Field(default_factory=BrollSettings)
    render: RenderSettings = Field(
        default_factory=lambda: RenderSettings(clip_seconds=10, polish=ReelPolish(enabled=True))
    )
    speech: SpeechSettings = Field(
        default_factory=lambda: SpeechSettings(voice_id="JBFqnCBsd6RMkjVDRZzb")
    )
    cartridge_version: Literal["v1", "v2"] = "v2"
    still_version: Literal["v1", "v2"] = "v2"
    video_version: Literal["v1", "v2"] = "v2"
    prompts: dict[str, str] = Field(default_factory=dict)
    games: tuple[StudioGame, ...] = ()
    source_story: RunId | None = None
    last_render: RunId | None = None
    last_narration: RunId | None = None

    @model_validator(mode="before")
    @classmethod
    def preserve_saved_world_review(cls, value):
        if isinstance(value, dict) and isinstance(value.get("story"), dict):
            value = {**value, "story": dict(value["story"])}
            value["story"].setdefault("world_review_prompt_version", None)
            value["story"].setdefault("world_review_reasoning_effort", None)
        return value

    @model_validator(mode="after")
    def four_games(self):
        if len(self.games) not in (0, 4):
            raise ValueError("A draft must contain either zero or exactly four games")
        if len({g.title.casefold() for g in self.games}) != len(self.games):
            raise ValueError("Game titles must be unique")
        if self.cartridge.image_size != "1536x1024":
            raise ValueError("Cartridge images must use landscape 1536x1024")
        if self.cartridge.image_background != "transparent":
            raise ValueError("Studio cartridges require transparent backgrounds")
        return self


class JobRequest(Model):
    draft_id: RunId
    revision: int = Field(ge=0)
    action: Action
    save_number: int | None = Field(default=None, ge=1, le=4)
    beat_number: int | None = Field(default=None, ge=1, le=2)
    regenerate: bool = False
    new_stories: bool = False

    @model_validator(mode="after")
    def selective_media(self):
        if self.beat_number is not None and self.action not in ("stills", "videos"):
            raise ValueError("A B-roll beat selector applies only to stills or videos")
        if self.new_stories and self.action != "full":
            raise ValueError("New stories selection applies to the finished reel action")
        if (self.save_number is not None or self.regenerate) and self.action not in (
            "cartridges",
            "stills",
            "videos",
        ):
            raise ValueError("Selective regeneration applies to media stages only")
        return self
