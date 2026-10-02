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
        elif request.save_number and stage in ("cartridges", "stills", "videos"):
            units = [f"save_{request.save_number:02}"]
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
