"""Observable work units, not estimates of provider time remaining."""

LABELS = {
    "concepts": "Develop world candidates",
    "diversity": "Review the four worlds together",
    "worlds": "Approve world simulations",
    "scripts": "Write and select narration",
    "cartridges": "Generate cartridge images",
    "stills": "Generate B-roll stills",
    "videos": "Animate B-roll clips",
    "render": "Assemble the base reel",
    "speech": "Record narration",
    "compose": "Mix voice, add captions and export MP4",
}


def plan(request, draft):
    stages = []
    action = request.action
    if action == "stories" or (action == "full" and (request.new_stories or not draft.games)):
        stages += ["concepts", "diversity", "worlds", "scripts"]
    elif action in ("scripts", "narrate", "full"):
        stages += ["scripts"]
    if action in ("cartridges", "full"):
        stages += ["cartridges"]
    if action in ("stills", "videos", "full"):
        stages += ["stills"]
    if action in ("videos", "full"):
        stages += ["videos"]
    if action in ("render", "narrate", "full"):
        stages += ["render"]
    if action in ("narrate", "full"):
        stages += ["speech", "compose"]
    result = []
    for stage in stages:
        units = [f"save_{i:02}" for i in range(1, 5)]
        if stage in ("diversity", "render", "compose"):
            units = ["reel"]
        elif stage == "speech":
            units = ["intro", *units]
            if draft.render.polish.enabled:
                units += [f"title_{i:02}" for i in range(1, 5)]
        elif request.save_number and stage in ("cartridges", "stills", "videos"):
            units = [f"save_{request.save_number:02}"]
        if stage in ("stills", "videos"):
            units = []
            for number in range(1, 5):
                if request.save_number and number != request.save_number:
                    continue
                new_worlds = not draft.games or request.new_stories
                game = draft.games[number - 1] if not new_worlds else None
                paired = (bool(draft.story.broll_prompt_version)
                          and draft.story.prompt_version != "v1") if new_worlds else bool(
                              game.broll_beats
                          )
                count = draft.broll.clips_per_save if paired else 1
                beats = [request.beat_number] if request.beat_number else range(1, count + 1)
                units += [f"save_{number:02}" + (f"_broll_{i:02}" if paired else "")
                          for i in beats]
        result.append(
            {"id": stage, "label": LABELS[stage], "units": dict.fromkeys(units, "pending")}
        )
    return {"stages": result, "completed": 0, "total": sum(len(s["units"]) for s in result)}


def update(progress, stage, unit, status):
    row = next((s for s in progress["stages"] if s["id"] == stage), None)
    if row and unit in row["units"]:
        row["units"][unit] = status
    progress["completed"] = sum(
        value == "completed" for s in progress["stages"] for value in s["units"].values()
    )
