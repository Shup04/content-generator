"""Editable render settings, ordered asset collections, and local render state."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.models import Artifact, Model, RunId, StageState, StageStatus, Text, utc_now
from save_reel.opener_style import OpenerStyle


class RenderGame(Model):
    title: Text
    values_file: Text
    cartridge_run_id: RunId
    broll_run_ids: Annotated[tuple[RunId, ...], Field(min_length=1)]


class RenderCollection(Model):
    description: Text
    template_version: Text
    image_background: Literal["transparent"]
    games: Annotated[tuple[RenderGame, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def unique_games(self) -> "RenderCollection":
        if len({game.cartridge_run_id for game in self.games}) != 4:
            raise ValueError("The collection must reference four distinct cartridges")
        return self


class BobMotion(Model):
    speed: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)]
    phase: Annotated[float, Field(allow_inf_nan=False)] = 0
    amplitude: Annotated[float, Field(ge=0, le=40, allow_inf_nan=False)]


class RenderSettings(Model):
    opener: OpenerStyle = Field(default_factory=OpenerStyle)
    width: Annotated[int, Field(ge=180, le=2160)] = 1080
    height: Annotated[int, Field(ge=320, le=3840)] = 1920
    fps: Literal[24, 30, 60] = 24
    intro_seconds: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 4
    countdown_seconds: Annotated[int, Field(ge=1, le=10)] = 5
    clip_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 5
    # Retained to load previous manifests; console UI reads only the opener tokens.
    background_color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#0d152b"
    heading: Text = "CHOOSE YOUR SAVE"
    intro_lines: Annotated[tuple[Text, ...], Field(min_length=1, max_length=3)] = (
        "You have died.",
        "Choose a game cartridge",
        "to be reincarnated into.",
    )
    show_clip_titles: bool = True
    countdown_volume: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.22
    bobbing: Annotated[tuple[BobMotion, ...], Field(min_length=4, max_length=4)] = (
        BobMotion(speed=1.1, phase=0, amplitude=7),
        BobMotion(speed=1.0, phase=1.4, amplitude=8),
        BobMotion(speed=1.2, phase=2.7, amplitude=6),
        BobMotion(speed=0.9, phase=4.0, amplitude=8),
    )

    @model_validator(mode="after")
    def valid_frame_geometry_and_timing(self) -> "RenderSettings":
        if self.width % 2 or self.height % 2 or self.width * 16 != self.height * 9:
            raise ValueError("Output dimensions must be even and exactly 9:16")
        for duration in (self.intro_seconds, self.clip_seconds):
            if abs(duration * self.fps - round(duration * self.fps)) > 1e-6:
                raise ValueError("Durations must correspond to a whole number of output frames")
        return self


class TimelineSegment(Model):
    kind: Literal["intro", "countdown", "broll"]
    start: float
    duration: float
    title: Text | None = None
    save_number: int | None = None
    clip_number: int | None = None
    source_run_id: RunId | None = None


def build_timeline(
    collection: RenderCollection, settings: RenderSettings
) -> tuple[TimelineSegment, ...]:
    timeline = [
        TimelineSegment(kind="intro", start=0, duration=settings.intro_seconds),
        TimelineSegment(
            kind="countdown", start=settings.intro_seconds, duration=settings.countdown_seconds
        ),
    ]
    start = settings.intro_seconds + settings.countdown_seconds
    for save_number, game in enumerate(collection.games, 1):
        for clip_number, run_id in enumerate(game.broll_run_ids, 1):
            timeline.append(
                TimelineSegment(
                    kind="broll",
                    start=start,
                    duration=settings.clip_seconds,
                    title=game.title,
                    save_number=save_number,
                    clip_number=clip_number,
                    source_run_id=run_id,
                )
            )
            start += settings.clip_seconds
    return tuple(timeline)


class RenderRun(Model):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["reel_render"] = "reel_render"
    run_id: RunId
    created_at: datetime = Field(default_factory=utc_now)
    collection: RenderCollection
    settings: RenderSettings
    timeline: tuple[TimelineSegment, ...]
    # The intro segment reserves this text/timing for a future narration provider.
    narration_text: Text
    tool_versions: dict[str, str]
    status: StageStatus = StageStatus.PENDING
    stages: dict[str, StageState] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
