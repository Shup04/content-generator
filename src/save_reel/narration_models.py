"""Manual narration scripts, speech alignment, and checkpointed narration state."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.models import Artifact, Model, RunId, StageState, StageStatus, Text, utc_now
from save_reel.render_models import RenderRun

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ScriptText = Annotated[Text, Field(max_length=500)]


class NarrationGame(Model):
    title: Text
    text: ScriptText


class NarrationScript(Model):
    intro: ScriptText
    games: Annotated[tuple[NarrationGame, ...], Field(min_length=4, max_length=4)]


class SpeechSettings(Model):
    voice_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
    model_id: Text = "eleven_multilingual_v2"
    speed: Annotated[float, Field(ge=0.7, le=1.2, allow_inf_nan=False)] = 1.0
    stability: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.6
    similarity_boost: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.75
    subtitles: bool = True
    max_tempo: Annotated[float, Field(ge=1, le=1.5, allow_inf_nan=False)] = 1.35


class SpeechAlignment(Model):
    characters: Annotated[tuple[str, ...], Field(min_length=1)]
    character_start_times_seconds: tuple[Seconds, ...]
    character_end_times_seconds: tuple[Seconds, ...]

    @model_validator(mode="after")
    def valid_timestamps(self) -> "SpeechAlignment":
        starts, ends = self.character_start_times_seconds, self.character_end_times_seconds
        if len(self.characters) != len(starts) or len(starts) != len(ends):
            raise ValueError("Speech alignment arrays must have matching lengths")
        if not any(c.strip() for c in self.characters):
            raise ValueError("Speech alignment must contain spoken text")
        if any(end < start for start, end in zip(starts, ends, strict=True)):
            raise ValueError("Speech alignment ends must follow starts")
        if list(starts) != sorted(starts) or list(ends) != sorted(ends) or ends[-1] <= 0:
            raise ValueError("Speech alignment times must be ordered and have positive duration")
        return self


class SpeechMetadata(Model):
    alignment: SpeechAlignment
    request_id: str | None = None
    character_cost: int | None = None


class NarrationCue(Model):
    cue_id: RunId
    text: ScriptText
    start: Seconds
    duration: Annotated[float, Field(gt=0.3, allow_inf_nan=False)]
    attempted: bool = False
    stage: StageState = Field(default_factory=StageState)
    audio: Artifact | None = None
    metadata: Artifact | None = None


def narration_cues(render: RenderRun, script: NarrationScript) -> tuple[NarrationCue, ...]:
    if [g.title for g in script.games] != [g.title for g in render.collection.games]:
        raise ValueError("Narration titles and order must match the rendered collection")
    intro = render.timeline[0]
    if intro.kind != "intro":
        raise ValueError("Render timeline must start with an introduction")
    cues = [
        NarrationCue(cue_id="intro", text=script.intro, start=intro.start, duration=intro.duration)
    ]
    for number, game in enumerate(script.games, 1):
        segments = [s for s in render.timeline if s.kind == "broll" and s.save_number == number]
        if not segments:
            raise ValueError(f"No rendered B-roll for save {number}")
        cues.append(
            NarrationCue(
                cue_id=f"save_{number:02}",
                text=game.text,
                start=segments[0].start,
                duration=sum(s.duration for s in segments),
            )
        )
    return tuple(cues)


class NarrationRun(Model):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["reel_narration"] = "reel_narration"
    run_id: RunId
    created_at: datetime = Field(default_factory=utc_now)
    source_render: RenderRun
    script: NarrationScript
    settings: SpeechSettings
    cues: tuple[NarrationCue, ...]
    status: StageStatus = StageStatus.PENDING
    stages: dict[str, StageState] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
