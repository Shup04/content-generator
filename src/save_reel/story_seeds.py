"""Reproducible, history-aware constraints, chosen before asking a language model."""

import hashlib
import json
import random
from collections import Counter
from importlib.resources import files

from save_reel.models import TemplateReference
from save_reel.story_models import ROLES, BroadWorldSeed, HistoryEntry, StorySettings, WorldSeed


def load_catalog(version: str = "v1") -> tuple[dict, TemplateReference]:
    source = files("save_reel.prompt_templates").joinpath("world_seeds", f"{version}.json")
    data = source.read_bytes()
    return json.loads(data), TemplateReference(
        name="world_seeds", version=version, sha256=hashlib.sha256(data).hexdigest()
    )


def make_seeds(
    random_seed: int,
    settings: StorySettings,
    history: tuple[HistoryEntry, ...],
    *,
    fixed: tuple[BroadWorldSeed | WorldSeed, ...] = (),
):
    catalog, reference = load_catalog(settings.seed_catalog_version)
    if settings.seed_catalog_version != "v1":
        return _broad_seeds(random_seed, catalog, history, fixed), reference
    rng = random.Random(random_seed)
    used_families = {s.setting_family for s in fixed}
    used_dangers = {s.danger_type for s in fixed}
    used_costs = {s.long_term_cost_type for s in fixed}
    counts = Counter(h.specific_setting for h in history)
    danger_counts = Counter(h.danger_type for h in history)
    cost_counts = Counter(h.long_term_cost_type for h in history)
    seeds = []
    social_states = ["alone", "small_human_group", "nonhuman_workers", "distant_neighbors"]
    rng.shuffle(social_states)
    for index, role in enumerate(ROLES):
        existing = next((s for s in fixed if s.role == role), None)
        if existing:
            seeds.append(existing)
            continue
        options = [s for s in catalog["settings"] if s["setting_family"] not in used_families]
        rng.shuffle(options)
        setting = min(options, key=lambda s: counts[s["specific_setting"]])
        dangers = [h for h in catalog["hazards"] if h["danger_type"] not in used_dangers]
        rng.shuffle(dangers)
        hazard = min(dangers, key=lambda h: danger_counts[h["danger_type"]])
        costs = [c for c in catalog["costs"] if c["type"] not in used_costs]
        rng.shuffle(costs)
        cost = min(costs, key=lambda c: cost_counts[c["type"]])
        seeds.append(
            WorldSeed(
                role=role,
                setting_family=setting["setting_family"],
                specific_setting=setting["specific_setting"],
                era=rng.choice(("2002", "2003", "2004", "2005", "2006")),
                time_of_day=rng.choice(
                    ("03:20 AM", "winter dusk", "late afternoon", "just before dawn")
                ),
                weather=rng.choice(("fine rain", "still cold air", "low cloud", "dry frost")),
                architecture_style="early-2000s public infrastructure",
                dominant_material=setting["dominant_material"],
                visual_motif=setting["visual_motif"],
                surface_emotion=("relief", "familiarity", "curiosity", "apprehension")[index],
                deeper_emotion=rng.choice(("dread", "resignation", "loneliness", "unease")),
                social_state=social_states[index],
                main_resource_problem=rng.choice(("food", "clean water", "warmth", "electricity")),
                danger_type=hazard["danger_type"],
                survival_rule_type=hazard["survival_rule_type"],
                impossible_property=rng.choice(catalog["impossible_properties"]),
                long_term_cost_type=cost["type"],
                palette=tuple(rng.choice(catalog["palettes"])),
                severity=rng.randint(*((4, 7), (5, 8), (6, 9), (8, 10))[index]),
            )
        )
        used_families.add(setting["setting_family"])
        used_dangers.add(hazard["danger_type"])
        used_costs.add(cost["type"])
    return tuple(seeds), reference


def _broad_seeds(random_seed, catalog, history, fixed):
    rng = random.Random(random_seed)
    used = {s.setting_family for s in fixed}
    counts = Counter(h.setting_family for h in history)
    seeds = []
    for role in ROLES:
        existing = next((s for s in fixed if s.role == role), None)
        if existing:
            seeds.append(existing)
            continue
        families = [f for f in catalog["setting_families"] if f not in used]
        rng.shuffle(families)
        family = min(families, key=lambda f: counts[f])
        used.add(family)
        seeds.append(
            BroadWorldSeed(
                role=role,
                setting_family=family,
                surface_emotion=rng.choice(catalog["surface_emotions"]),
                deeper_emotion=rng.choice(catalog["deeper_emotions"]),
                resource_pressure=rng.choice(catalog["resource_pressures"]),
                severity=rng.randint(7, 9) if role == ROLES[3] else rng.randint(4, 8),
                era=rng.choice(catalog["eras"]),
                time_of_day=rng.choice(catalog["times"]),
                weather=rng.choice(catalog["weather"]),
            )
        )
    return tuple(seeds)
