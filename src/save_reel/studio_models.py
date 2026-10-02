"""Editable Studio drafts. Generated run manifests remain immutable inputs."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.media_models import (
    BrollSettings,
    BrollValues,
    CartridgeValues,
    ImageSettings,
    MotionValues,
)
from save_reel.models import Model, RunId, Text
from save_reel.narration_models import SpeechSettings
from save_reel.render_models import RenderSettings
from save_reel.story_models import StorySettings

Action = Literal["stories", "cartridges", "stills", "videos", "render", "narrate", "full"]


class StudioGame(Model):
    title: Text
    narration: Annotated[Text, Field(max_length=1000)]
    cartridge: CartridgeValues
    environment: BrollValues
    motion: MotionValues
    cartridge_run: RunId | None = None
    broll_run: RunId | None = None

    @model_validator(mode="after")
    def matching_titles(self):
        if self.title != self.cartridge.title or self.title != self.environment.game_title:
            raise ValueError("Game, cartridge and environment titles must match")
        return self


class StudioDraft(Model):
    draft_id: RunId
    revision: int = Field(default=0, ge=0)
    name: Text = "Untitled reel"
    theme: Text = "strange worlds you must survive inside permanently"
    provider: Literal["mock", "openai"] = "mock"
    seed: int | None = None
    parallel_saves: int = Field(default=4, ge=1, le=4)
    preset_revision: RunId | None = None
    story: StorySettings = Field(default_factory=StorySettings)
    cartridge: ImageSettings = Field(
        default_factory=lambda: ImageSettings(image_background="transparent")
    )
    broll: BrollSettings = Field(default_factory=lambda: BrollSettings(duration=10))
    render: RenderSettings = Field(
        default_factory=lambda: RenderSettings(clip_seconds=23, loop_short_clips=True)
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
    regenerate: bool = False
    new_stories: bool = False

    @model_validator(mode="after")
    def selective_media(self):
        if self.new_stories and self.action != "full":
            raise ValueError("New stories selection applies to the finished reel action")
        if (self.save_number is not None or self.regenerate) and self.action not in (
            "cartridges",
            "stills",
            "videos",
        ):
            raise ValueError("Selective regeneration applies to media stages only")
        return self
