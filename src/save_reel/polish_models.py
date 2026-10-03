"""Opt-in pacing and shared reveal UI tokens; old renders keep their saved behavior."""

from pydantic import Field

from save_reel.models import Model
from save_reel.opener_style import Color, Opacity, Size
from save_reel.story_tiers import TIER_INTROS, TIERS


class RevealStyle(Model):
    accent: Color = "#79d9e8"
    panel: Color = "#060d13"
    panel_opacity: Opacity = 0.78
    border_opacity: Opacity = 0.38
    margin: Size = 64
    lower_y: Size = 1380
    lower_height: Size = 132
    label_size: Size = 26
    title_size: Size = 52
    caption_y: Size = 1570
    caption_size: Size = 44
    caption_padding: Size = 22
    caption_wrap: int = Field(default=32, ge=20, le=40)
    card_width: Size = 930
    card_height: Size = 620
    card_y: Size = 580
    card_title_y: Size = 1270


class ReelPolish(Model):
    enabled: bool = False
    title_seconds: float = Field(default=1.25, ge=0.8, le=1.5, allow_inf_nan=False)
    max_slowdown: float = Field(default=1.5, ge=1, le=4, allow_inf_nan=False)
    allow_still_fallback: bool = True
    still_position: str = Field(default="after", pattern=r"^(before|after)$")
    still_zoom: float = Field(default=0.035, ge=0, le=0.12, allow_inf_nan=False)
    transition_seconds: float = Field(default=0.125, ge=0, le=0.5, allow_inf_nan=False)
    lower_third_seconds: float = Field(default=3, ge=0, le=10, allow_inf_nan=False)
    tier_intros: dict[str, str] = Field(default_factory=lambda: dict(TIER_INTROS))
    style: RevealStyle = Field(default_factory=RevealStyle)

    def tier_intro(self, tier):
        if tier not in TIERS or not self.tier_intros.get(tier, "").strip():
            raise ValueError("Choose a valid survival tier and provide its narrator introduction")
        return self.tier_intros[tier].strip()
