"""Relative survival outcomes, independent of visual roles or slot numbers."""

from enum import StrEnum


class SurvivabilityTier(StrEnum):
    BEST = "best"
    GOOD = "good"
    RISKY = "risky"
    BAD = "bad"


TIERS = tuple(SurvivabilityTier)
TIER_INTROS = {
    "best": "You have once again chosen perfectly.",
    "good": "You made a decent choice.",
    "risky": "This one will be harder than it looks.",
    "bad": "You picked badly.",
}
TIER_SEVERITY = {"best": (2, 3), "good": (4, 5), "risky": (6, 7), "bad": (8, 10)}
