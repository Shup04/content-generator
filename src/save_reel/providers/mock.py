"""Deterministic, offline concepts; no credentials or network access needed."""

from save_reel.models import (
    CartridgeSpec,
    ConceptRequest,
    EffectsSpec,
    EnvironmentSpec,
    ReelConcept,
    SaveGameConcept,
)


class MockConceptProvider:
    name = "mock"

    def generate_concept(self, request: ConceptRequest) -> ReelConcept:
        worlds = (
            {
                "title": "Mosslight Station",
                "biome": "temperate rainforest",
                "setting": "an abandoned railway platform among enormous trees",
                "focal_point": "a moss-covered clock tower",
                "time_of_day": "dawn",
                "weather": "light rain",
                "lighting": "soft green light through the canopy",
                "palette": ("moss green", "warm amber", "slate gray"),
                "mood": "quiet discovery",
                "details": ("fern-covered tracks", "glowing station lanterns"),
                "symbol": "fern",
                "particles": ("drifting pollen",),
                "atmosphere": ("low forest mist",),
                "motion": ("gently swaying leaves",),
                "world_scene": (
                    "An abandoned railway platform among enormous rainforest trees, with a "
                    "moss-covered clock tower, fern-covered tracks, and glowing station lanterns. "
                    "Dawn light filters through the canopy into low forest mist."
                ),
                "shot_composition": (
                    "Eye-level view along the tracks toward the clock tower, framed by trees; "
                    "keep the clock tower and platform inside the central portrait area."
                ),
                "key_surfaces": ("platform concrete", "tree bark", "mossy clock-tower stone"),
            },
            {
                "title": "Tidal Archive",
                "biome": "shallow tropical sea",
                "setting": "a submerged library beneath a glass dome",
                "focal_point": "a spiral staircase surrounded by coral",
                "time_of_day": "midday",
                "weather": "clear skies above the water",
                "lighting": "rippling sunlight refracted through water",
                "palette": ("turquoise", "coral pink", "pearl white"),
                "mood": "serene wonder",
                "details": ("shell-encrusted shelves", "schools of silver fish"),
                "symbol": "nautilus shell",
                "particles": ("suspended sea dust",),
                "atmosphere": ("blue underwater haze",),
                "motion": ("slowly drifting fish",),
                "world_scene": (
                    "A submerged library beneath a glass dome in a shallow tropical sea. "
                    "A spiral staircase rises among coral and shell-encrusted shelves, "
                    "while silver fish pass through blue underwater haze."
                ),
                "shot_composition": (
                    "View from the library entrance with the staircase centered vertically "
                    "and nearby shelves framing both sides."
                ),
                "key_surfaces": ("library floor tiles", "coral", "shelf wood", "dome glass"),
            },
            {
                "title": "Ember Observatory",
                "biome": "volcanic highlands",
                "setting": "a brass observatory at the edge of a dormant crater",
                "focal_point": "a giant telescope aimed at a ringed moon",
                "time_of_day": "night",
                "weather": "cold and cloudless",
                "lighting": "orange fissure glow beneath cool moonlight",
                "palette": ("charcoal", "ember orange", "deep violet"),
                "mood": "solemn anticipation",
                "details": ("obsidian paths", "weathered star charts"),
                "symbol": "ringed moon",
                "particles": ("faint floating sparks",),
                "atmosphere": ("thin geothermal steam",),
                "motion": ("slowly rising steam",),
                "world_scene": (
                    "A brass observatory at the edge of a dormant volcanic crater at night. "
                    "A giant telescope points at a ringed moon above obsidian paths and "
                    "weathered star charts, with orange fissure glow and thin geothermal steam."
                ),
                "shot_composition": (
                    "Low viewpoint along an obsidian path, with the telescope in the middle "
                    "of the frame and the ringed moon above it."
                ),
                "key_surfaces": ("obsidian path", "brass telescope", "crater rock", "star charts"),
            },
            {
                "title": "Cloud Orchard",
                "biome": "floating alpine islands",
                "setting": "an orchard on a small island above a sea of clouds",
                "focal_point": "a golden fruit tree beside a rope bridge",
                "time_of_day": "sunset",
                "weather": "a gentle high-altitude breeze",
                "lighting": "warm rim light and lavender shadows",
                "palette": ("gold", "lavender", "sky blue"),
                "mood": "hopeful solitude",
                "details": ("hanging wind chimes", "distant floating gardens"),
                "symbol": "winged apple",
                "particles": ("windblown petals",),
                "atmosphere": ("soft cloud banks",),
                "motion": ("gently swinging wind chimes",),
                "world_scene": (
                    "An orchard on a small floating alpine island above a sea of clouds. "
                    "A golden fruit tree stands beside a rope bridge and hanging wind chimes, "
                    "with distant floating gardens against a lavender sunset."
                ),
                "shot_composition": (
                    "View from the rope bridge toward the golden tree, centered in the "
                    "portrait frame with cloud banks visible beneath the island."
                ),
                "key_surfaces": ("bridge planks", "tree bark", "grass", "island rock"),
            },
        )
        saves = []
        for world in worlds:
            environment = EnvironmentSpec(
                **{key: world[key] for key in EnvironmentSpec.model_fields}
            )
            saves.append(
                SaveGameConcept(
                    title=world["title"],
                    summary=f"{request.theme}: {world['setting']}.",
                    environment=environment,
                    cartridge=CartridgeSpec(
                        shell_color=environment.palette[0],
                        label_title=world["title"],
                        label_symbol=world["symbol"],
                        material="translucent recycled plastic",
                        wear="light edge scuffs",
                    ),
                    effects=EffectsSpec(
                        ambient_particles=world["particles"],
                        atmospheric_effects=world["atmosphere"],
                        motion_cues=world["motion"],
                    ),
                )
            )
        return ReelConcept(title=f"Choose Your Save: {request.theme}", saves=tuple(saves))
