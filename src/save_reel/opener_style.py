"""Reusable console UI tokens. Coordinates/sizes are in a 1080x1920 design space."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from save_reel.models import Model

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Opacity = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Size = Annotated[int, Field(ge=1, le=1920)]

INTRO_CAPTIONS = (
    "You have died",
    "You must pick a game cartridge to be reincarnated into",
)
INTRO_SCRIPT = ". ".join(INTRO_CAPTIONS) + "."


class OpenerStyle(Model):
    # None selects the bundled fonts. Custom paths are copied into each render run.
    display_font_file: str | None = None
    system_font_file: str | None = None
    background: Color = "#040508"
    text_color: Color = "#e4e8eb"
    system_color: Color = "#b6c1c9"
    star_color: Color = "#ffffff"
    separation_color: Color = "#aab8c5"
    system_opacity: Opacity = 0.78
    bracket_opacity: Opacity = 0.20
    separation_opacity: Opacity = 0.075
    star_opacity_min: Opacity = 0.18
    star_opacity_max: Opacity = 0.65
    star_count: Annotated[int, Field(ge=0, le=160)] = 72
    star_seed: int = 2003
    star_radius_min: Annotated[float, Field(gt=0, le=4)] = 0.6
    star_radius_max: Annotated[float, Field(gt=0, le=4)] = 1.8
    star_drift: Annotated[float, Field(ge=0, le=12)] = 5.0
    star_drift_speed: Annotated[float, Field(gt=0, le=1)] = 0.12
    overscan: Size = 24
    card_width: Size = 492
    card_height: Size = 328
    column_x: tuple[int, int] = (30, 558)
    row_y: tuple[int, int] = (635, 1055)
    label_gap: Size = 38
    label_size: Size = 25
    bracket_length: Size = 20
    bracket_stroke: Size = 2
    bracket_inset: Size = 8
    halo_padding: Size = 30
    choice_label_y: Size = 250
    choice_label_size: Size = 25
    countdown_y: Size = 310
    countdown_size: Size = 152
    intro_center_y: Size = 900
    intro_first_size: Size = 76
    intro_second_size: Size = 58
    intro_wrap: Annotated[int, Field(ge=16, le=40)] = 32
    intro_alignment: Literal["C", "L", "R"] = "C"
    intro_line_spacing: Size = 16

    @model_validator(mode="after")
    def ordered_ranges(self) -> "OpenerStyle":
        if (
            self.star_opacity_min > self.star_opacity_max
            or self.star_radius_min > self.star_radius_max
        ):
            raise ValueError("Star minimum values cannot exceed their maximum values")
        if self.overscan < self.star_drift + 2:
            raise ValueError("Starfield overscan must exceed drift by at least two pixels")
        return self
