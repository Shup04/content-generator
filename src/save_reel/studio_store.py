"""Local drafts, validated template snapshots, and read-only run discovery."""

import json
import re
import tempfile
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from save_reel.broll_beats import StudioBrollBeat
from save_reel.models import RunId, utc_now
from save_reel.narration_models import NarrationRun
from save_reel.prompting import (
    BrollStillPromptCompiler,
    BrollVideoPromptCompiler,
    CartridgePromptCompiler,
)
from save_reel.storage import RunStore
from save_reel.story_export import visual_values
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prompting import load_prompts, load_travelogue_prompts
from save_reel.story_tiers import TIERS
from save_reel.studio_models import NarrationOrigin, StudioDraft, StudioGame, narration_digest

MEDIA_FILES = {
    "reel.mp4",
    "cartridge.png",
    "broll_still.png",
    "broll_video.mp4",
    "preview.png",
    "story_review.txt",
    "subtitles.srt",
    "broll_01_still.png", "broll_02_still.png", "broll_01.mp4", "broll_02.mp4",
}

# Presets contain reusable production choices, never reel content or generated assets.
PRESET_FIELDS = {
    "story",
    "cartridge",
    "broll",
    "render",
    "speech",
    "prompts",
    "parallel_saves",
    "speech_parallel_saves",
    "cartridge_version",
    "still_version",
    "video_version",
}


def is_finished_reel(folder: Path) -> bool:
    try:
        run = NarrationRun.model_validate_json((folder / "narration.json").read_text())
        artifact = run.artifacts.get("reel")
        expected = {"intro", "save_01", "save_02", "save_03", "save_04"}
        if run.source_render.settings.polish.enabled:
            expected |= {f"title_{i:02}" for i in range(1, 5)}
        return bool(
            run.run_id == folder.name
            and run.status == "completed"
            and all(
                run.stages.get(k) and run.stages[k].status == "completed"
                for k in ("speech", "compose")
            )
            and {c.cue_id for c in run.cues}
            == expected
            and len(run.cues) == len(expected)
            and all(
                c.stage.status == "completed"
                and c.audio
                and c.metadata
                and (folder / c.audio.path).is_file()
                and (folder / c.metadata.path).is_file()
                for c in run.cues
            )
            and artifact
            and artifact.path == "reel.mp4"
            and (folder / artifact.path).resolve().parent == folder.resolve()
            and (folder / artifact.path).stat().st_size > 0
        )
    except (ValueError, OSError):
        return False


def identifier(value: str) -> str:
    return TypeAdapter(RunId).validate_python(value)


def write_json(path: Path, value) -> None:
    RunStore._write_atomic(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def bundled_prompts() -> dict[str, str]:
    root = files("save_reel.prompt_templates")
    return {
        f"{folder.name}/{file.name}": file.read_text(encoding="utf-8")
        for folder in root.iterdir()
        if folder.is_dir()
        for file in folder.iterdir()
        if re.fullmatch(r"v[1-9][0-9]*\.txt", file.name)
    }


def snapshot_prompts(draft: StudioDraft, target: Path) -> None:
    allowed = bundled_prompts()
    if set(draft.prompts) != set(allowed):
        raise ValueError("Prompt files must match the bundled template catalog")
    for name, text in draft.prompts.items():
        if not text.strip() or len(text) > 100_000:
            raise ValueError(f"{name}: use between 1 and 100,000 characters")
        RunStore._write_atomic(target / name, text.encode())


def compile_game(draft: StudioDraft, game: StudioGame, root: Path, beat_number=1) -> dict:
    environment, motion = game.clip_values(beat_number)
    env = environment.model_dump(by_alias=True)
    return {
        "cartridge": CartridgePromptCompiler(draft.cartridge_version, prompts_dir=root).render(
            game.cartridge.model_dump(by_alias=True, exclude_none=True)
        ),
        "still": BrollStillPromptCompiler(draft.still_version, prompts_dir=root).render(env),
        "video": BrollVideoPromptCompiler(draft.video_version, prompts_dir=root).render(
            {**env, **motion.model_dump(by_alias=True)}
        ),
    }


def validate_prompts(draft: StudioDraft, root: Path) -> None:
    snapshot_prompts(draft, root)
    load_prompts(draft.story.prompt_version, root, draft.story.tier_prompt_version,
                 draft.story.broll_prompt_version, draft.story.world_review_prompt_version)
    if draft.story.prompt_version == "v3" and draft.story.travelogue:
        load_travelogue_prompts(draft.story.travelogue.prompt_version, root)
    # Validate visual placeholders even before a draft has worlds.
    placeholders = {
        "TITLE",
        "SHELL_MATERIAL_COLOR",
        "SHAPE_LANGUAGE",
        "MOTIFS_GROOVES_SURFACE_DETAILS",
        "OBJECT_MOOD",
        "LABEL_SCENE_DESCRIPTION",
        "HINT_SCENE_DESCRIPTION",
        "GAME_TITLE",
        "WORLD_SCENE",
        "COLOUR_PALETTE",
        "MOOD",
        "SHOT_COMPOSITION",
        "KEY_SURFACES",
        "CAMERA_MOTION",
        "ENVIRONMENT_MOTION",
        "AUDIO",
    }
    for compiler, version in (
        (CartridgePromptCompiler, draft.cartridge_version),
        (BrollStillPromptCompiler, draft.still_version),
        (BrollVideoPromptCompiler, draft.video_version),
    ):
        compiler(version, prompts_dir=root).render(dict.fromkeys(placeholders, "preview"))
    for game in draft.games:
        for beat in game.clip_numbers():
            if len(compile_game(draft, game, root, beat)["video"].text) > 7000:
                raise ValueError(f"{game.title}: compiled video prompt exceeds 7000 characters")


@lru_cache(maxsize=512)
def _run_summary(path: str, stamp: int, size: int) -> dict:
    raw = json.loads(Path(path).read_text())
    folder = Path(path).parent
    kind = Path(path).stem
    titles = []
    if kind == "story":
        titles = [s["final"]["brief"]["title"] for s in raw.get("saves", []) if s.get("final")]
    elif kind == "cartridge":
        titles = [raw["values"].get("TITLE", raw["values"].get("title", ""))]
    elif kind == "broll":
        titles = [raw["values"].get("GAME_TITLE", raw["values"].get("game_title", ""))]
    else:
        titles = [g["title"] for g in raw.get("script", raw.get("collection", {})).get("games", [])]
    return {
        "run_id": folder.name,
        "kind": kind,
        "status": raw.get("status", "unknown"),
        "created_at": raw.get("created_at", ""),
        "titles": titles,
        "files": sorted(name for name in MEDIA_FILES if (folder / name).is_file()),
    }


class StudioStore:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.runs = self.project / "runs"
        self.root = self.runs / ".studio"
        self.drafts = self.root / "drafts"
        self.jobs = self.root / "jobs"
        self.presets = self.root / "presets"
        self.revisions = self.root / "revisions"
        self.drafts.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.presets.mkdir(parents=True, exist_ok=True)
        self.revisions.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        path = self.runs / identifier(run_id)
        if path.resolve().parent != self.runs.resolve():
            raise ValueError("Run must be inside this project's runs directory")
        return path

    def load(self, draft_id: str) -> StudioDraft:
        draft = StudioDraft.model_validate_json(
            (self.drafts / f"{identifier(draft_id)}.json").read_text()
        )
        draft.prompts = {**bundled_prompts(), **draft.prompts}
        self.annotate_narration(draft)
        return draft

    def annotate_narration(self, draft: StudioDraft) -> None:
        """Recover old drafts' actual script provenance, independent of model selectors."""
        source = None
        if draft.source_story and (
            any(g.narration_origin is None for g in draft.games)
            or all(g.survivability_tier is None for g in draft.games)
        ):
            source = StoryWorkflow.load(self.run_dir(draft.source_story))
        originals = games_from_story(source) if source else ()
        if originals and all(g.survivability_tier is None for g in draft.games):
            for game in draft.games:
                original = next((g for g in originals if g.title == game.title), None)
                if original:
                    game.survivability_tier = original.survivability_tier
        for game in draft.games:
            digest = narration_digest(game.narration)
            if game.narration_origin and game.narration_origin.text_sha256 == digest:
                continue
            original = next((g for g in originals if g.title == game.title
                             and narration_digest(g.narration) == digest), None)
            game.narration_origin = original.narration_origin if original else NarrationOrigin(
                provider="manual", text_sha256=digest,
            )

    def save(self, draft: StudioDraft, *, check_revision=True) -> StudioDraft:
        draft.prompts = {**bundled_prompts(), **draft.prompts}
        path = self.drafts / f"{identifier(draft.draft_id)}.json"
        if (
            check_revision
            and path.exists()
            and self.load(draft.draft_id).revision != draft.revision
        ):
            raise ValueError("This draft changed in another tab. Reload before saving.")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            validate_prompts(draft, Path(directory))
        self.annotate_narration(draft)
        data = draft.model_dump(mode="json")
        data["revision"] += 1
        saved = StudioDraft.model_validate(data)
        # Keep a settings/prompt revision before every edit; runs keep separate snapshots.
        if path.exists():
            previous = self.load(draft.draft_id)
            revision_path = self.revisions / draft.draft_id / f"{previous.revision}.json"
            if not revision_path.exists():
                write_json(
                    revision_path,
                    {
                        "revision": previous.revision,
                        "created_at": utc_now().isoformat(),
                        "settings": previous.model_dump(mode="json", include=PRESET_FIELDS),
                    },
                )
        write_json(path, saved.model_dump(mode="json"))
        return saved

    def list_revisions(self, draft_id):
        return [
            {k: v for k, v in json.loads(p.read_text()).items() if k != "settings"}
            for p in sorted(
                (self.revisions / identifier(draft_id)).glob("*.json"),
                key=lambda p: int(p.stem),
                reverse=True,
            )
        ]

    def restore_revision(self, draft, revision, prompt=None):
        if not isinstance(revision, int) or revision < 0:
            raise ValueError("Invalid revision")
        saved = json.loads(
            (self.revisions / identifier(draft.draft_id) / f"{revision}.json").read_text()
        )
        data = draft.model_dump(mode="json")
        if prompt is not None:
            if prompt not in data["prompts"]:
                raise ValueError("Unknown template")
            data["prompts"][prompt] = saved["settings"]["prompts"][prompt]
        else:
            data.update(saved["settings"])
            data["preset_revision"] = None
        return self.save(StudioDraft.model_validate(data))

    def save_preset(self, draft, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Preset name must contain 1–100 characters")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            validate_prompts(draft, Path(directory))
        preset = {
            "id": f"preset-{uuid4().hex[:12]}",
            "name": name.strip(),
            "created_at": utc_now().isoformat(),
            "settings": draft.model_dump(mode="json", include=PRESET_FIELDS),
        }
        write_json(self.presets / f"{preset['id']}.json", preset)
        return {k: v for k, v in preset.items() if k != "settings"}

    def list_presets(self):
        return [
            {k: v for k, v in json.loads(p.read_text()).items() if k != "settings"}
            for p in sorted(
                self.presets.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        ]

    def apply_preset(self, draft, preset_id):
        preset = json.loads((self.presets / f"{identifier(preset_id)}.json").read_text())
        data = {**draft.model_dump(mode="json"), **preset["settings"], "preset_revision": preset_id}
        return self.save(StudioDraft.model_validate(data))

    def create(self, name="Untitled reel", source: StudioDraft | None = None) -> StudioDraft:
        data = source.model_dump(mode="json") if source else {"prompts": bundled_prompts()}
        data.update(draft_id=f"draft-{uuid4().hex[:12]}", revision=0, name=name)
        return self.save(StudioDraft.model_validate(data))

    def list_drafts(self) -> list[dict]:
        result = []
        for path in sorted(
            self.drafts.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            draft = self.load(path.stem)
            result.append(
                {
                    "draft_id": draft.draft_id,
                    "name": draft.name,
                    "revision": draft.revision,
                    "games": len(draft.games),
                }
            )
        return result

    def import_story(self, run_id: str) -> StudioDraft:
        story = StoryWorkflow.load(self.run_dir(run_id))
        if not all(s.final for s in story.saves):
            raise ValueError("Only a story with four finished saves can be imported")
        draft = StudioDraft(
            draft_id=f"draft-{uuid4().hex[:12]}",
            name=run_id,
            source_story=run_id,
            provider=story.provider,
            story=story.settings,
            theme=story.request.theme,
            prompts=bundled_prompts(),
            games=games_from_story(story),
        )
        # Only attach byte-for-byte matching assets; importing does not regenerate anything.
        from save_reel.broll import BrollPipeline
        from save_reel.cartridge import CartridgePipeline

        for row in self.list_runs():
            try:
                store = RunStore(self.run_dir(row["run_id"]))
                for game in draft.games:
                    if row["kind"] == "cartridge" and not game.cartridge_run:
                        if CartridgePipeline.load(store).values == game.cartridge:
                            game.cartridge_run = row["run_id"]
                    if row["kind"] == "broll":
                        roll = BrollPipeline.load(store)
                        for number in game.clip_numbers():
                            env, motion = game.clip_values(number)
                            if (not game.clip_run(number) and roll.values == env
                                    and roll.motion == motion):
                                game.set_clip_run(number, row["run_id"])
            except (ValueError, OSError):
                continue
        return self.save(draft)

    def list_runs(self) -> list[dict]:
        result = []
        folders = sorted(
            (p for p in self.runs.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for folder in folders:
            try:
                self.run_dir(folder.name)
                for name in ("narration", "render", "story", "cartridge", "broll"):
                    path = folder / f"{name}.json"
                    if path.is_file():
                        stat = path.stat()
                        row = dict(_run_summary(str(path), stat.st_mtime_ns, stat.st_size))
                        # Check files on every listing, even when the manifest is cached.
                        row["files"] = sorted(
                            name for name in MEDIA_FILES if (folder / name).is_file()
                        )
                        row["finished"] = name == "narration" and is_finished_reel(folder)
                        row["name"] = ""
                        if name in ("narration", "render"):
                            raw = json.loads(path.read_text())
                            source = raw.get("source_render", raw)
                            row["name"] = source.get("collection", {}).get("description", "")
                        result.append(row)
                        break
            except (ValueError, OSError, KeyError):
                continue
        return result


def games_from_story(story) -> tuple[StudioGame, ...]:
    games = []
    for slot in story.saves:
        cart, env, motion = visual_values(slot)
        games.append(
            StudioGame(
                title=slot.final.brief.title,
                narration=" ".join(slot.final.brief.narration),
                survivability_tier=slot.final.brief.survivability_tier,
                narration_origin=NarrationOrigin(
                    provider=slot.narration_provider or story.provider,
                    model=slot.narration_model or (
                        story.settings.model_for("narration_prose_candidate")
                        if story.provider == "openai" else story.model
                    ),
                    source_run=story.run_id,
                    text_sha256=narration_digest(" ".join(slot.final.brief.narration)),
                ),
                cartridge=cart,
                environment=env,
                motion=motion,
                broll_beats=tuple(StudioBrollBeat(**b.model_dump())
                                  for b in slot.final.environment.broll_beats or ()),
            )
        )
    if all(g.survivability_tier is None for g in games):
        # Legacy worlds cannot be redesigned without invalidating their visuals.
        # Rank saved severity, using title digest for stable ties independent of slot.
        ranked = sorted(zip(games, story.saves, strict=True), key=lambda pair: (
            pair[1].final.brief.severity, narration_digest(pair[0].title)
        ))
        for tier, (game, _) in zip(TIERS, ranked, strict=True):
            game.survivability_tier = tier
    return tuple(games)
