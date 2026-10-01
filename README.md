# save-reel

The initial foundation for short-form **Choose Your Save** videos. This milestone
creates one reel with exactly four save games, prepares four label prompts and
four B-roll still prompts, and saves a JSON manifest. The mock workflow runs locally
without API keys or network access.

Optional real workflows generate full cartridge renders with GPT Image 2.5,
or generate B-roll stills and animate them with MiniMax H3. The local `render`
command combines saved cartridges and B-roll into a finished vertical MP4 with
FFmpeg. Separate flat label image generation, Blender, narration, and social
publishing are not implemented.

## How the prompts fit your video

Each fictional game has one shared title, world description, colour palette, and
mood. Those values fill the label and B-roll templates; cartridge renders add
structured shell design values:

1. **Cartridge label:** `prompts/label/v1.txt` describes flat 3:2 artwork with the
   game title. Later, an image provider will generate the artwork, which can be
   applied as the cartridge label texture in your Blender scene.
2. **B-roll still:** `prompts/broll_still/v2.txt` describes a 9:16 game screenshot
   from that same world. It also specifies shot composition and key surfaces.
   The `generate-broll` command generates this still and sends it to MiniMax H3
   as the first frame of a video.
3. **Full cartridge render:** `prompts/cartridge/v2.txt` renders an isolated shell
   with a mysterious teaser label on transparency. `generate-cartridge` produces
   this image from explicit shell material, shape, molded details, mood, and a hint.

The current video uses **four transparent cartridge images and four B-roll
clips**, each animated from its own still. The `render` command animates the
cartridge cutouts in FFmpeg and then shows the B-roll. A second clip per story
can be added to the collection later. The `create` command prepares the eight
text prompts; it does not generate images or clips. The original supplied
wording remains in the `v1` templates.
The revised B-roll templates use `v2`; all versions use `[VARIABLE]` placeholders.

## Install

Requires Python 3.11 or newer. From the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

For a regular installation without development tools, use `python -m pip install .`.
Pydantic v2 is the only dependency for the mock workflow. Installation may download dependencies;
running the mock workflow does not.

## Render the reel locally

Requires FFmpeg and ffprobe on `PATH`, with the `libx264` encoder and `drawtext`
filter available. The first pass was verified with FFmpeg 9.0.1. The renderer
uses an installed Liberation Sans, DejaVu Sans, or Arial bold font; pass
`--font-file /path/to/font.ttf` to select a different font. No API keys or optional
media Python dependencies are required for rendering existing assets.

With the four cartridge and B-roll runs already generated:

```sh
save-reel render \
  --collection examples/cartridges/v2/collection.json \
  --settings examples/reel-first-pass.json \
  --run-id reel-first-pass
```

The command prints `runs/reel-first-pass/reel.mp4`. The default output is
1080×1920, 24 fps, H.264 with AAC audio, and has this 29-second timeline:

| Time | Content |
| --- | --- |
| 0–4 seconds | Four transparent cartridges in a 2×2 grid, introduction text |
| 4–9 seconds | Same animated grid, countdown from 5 to 1, one tone per second |
| 9–14 seconds | DEEP END |
| 14–19 seconds | SUNROOM LINE |
| 19–24 seconds | VELVET ORCHARD |
| 24–29 seconds | SIGNAL 03 |

The cartridges use the four supplied sine motions, with amplitudes in pixels at
1080-pixel width and proportional scaling for smaller renders. Each reveal starts
with a short save-number/title caption. Clips are center-cropped to exact 9:16,
resized, and trimmed to five seconds. Original clip audio is removed. Only the
locally synthesized countdown tones play; the introduction and B-roll are silent
until narration and your supplied music are added.

Edit `examples/reel-first-pass.json` to change the introduction duration, countdown,
clip duration, bobbing, text, background, captions, sound volume, or output size.
Durations must fit whole frames and dimensions must be even and exactly 9:16.
The introduction timing and narration text are stored explicitly for a future
ElevenLabs narration stage; no narration API is connected in this step. Additional
B-roll run IDs in a game's `broll_run_ids` list play consecutively before the next
game, each for `clip_seconds`.

The collection sets save order and references completed `cartridge.json` and
`broll.json` runs. The renderer checks titles, transparency settings, checksums,
and available clip duration before creating a render run. `values_file` is retained
as collection metadata; rendering reads the saved media runs. `--source-runs-dir`
selects their root directory; `--runs-dir` selects the output root. Both default to
`runs/` relative to the current directory. Use a fresh run ID for each revision;
existing runs are never overwritten. `--ffmpeg` and `--ffprobe` accept custom
executable paths.

```text
runs/<render_run_id>/
  reel.mp4             # Final output, saved after validation
  render.json          # Collection, settings, timeline, stages, artifact checksums
  probe.json           # Verified output media properties
  inputs/save_01/...   # Copies of source cutout/clip assets, through save_04
  segments/...         # Encoded intro and individual reveals
  audio/countdown.wav  # Countdown track on the complete reel timeline
  filters/...          # Saved FFmpeg filter graphs
  text/...             # Captions passed as literal text files
  font.ttf
  command_*.json       # Exact FFmpeg argument lists
  ffmpeg_*.log
  run.log
```

The run is self-contained and retains its inputs for future editing. Failed
renders record the failed stage and retain diagnostic logs; start a new run after
fixing the cause. `render.json` describes assembly and remains separate from the
creative `manifest.json` produced by `create`. Rendering calls no paid providers.

## Generate cartridge renders

Install the optional dependencies with `python -m pip install -e '.[media]'`
and set `OPENAI_API_KEY` in your environment or local `.env`. MiniMax is not used
by these commands.

```sh
save-reel generate-cartridge \
  --values examples/cartridges/v2/deep-end.json \
  --run-id deep-end-cartridge-v2
```

This makes one paid image request. Defaults are `gpt-image-2.5-sunburst`, medium
quality, template `v2`, and a 1536×1024 PNG with a transparent background.
The API receives `background="transparent"`; the adapter verifies that the PNG
contains both fully transparent pixels and visible content. Transparency is not
left to prompt wording alone. B-roll images continue to use opaque backgrounds.
Use `--image-quality high` or `--image-model gpt-image-2.5-flare` to change image settings.
Use a fresh run ID for a new generation. `--env-file` selects a different local
environment file.

The current four designs are in `examples/cartridges/v2/`: DEEP END (a pool ladder
glimpse), SUNROOM LINE (a sunlit ticket), VELVET ORCHARD (one glowing peach), and
SIGNAL 03 (a slit of light and disconnected square marks). Their labels hint at
the story while leaving the full settings and outcomes for the B-roll reveal.
The values use `HINT_SCENE_DESCRIPTION`, with no studio-background instructions.
That directory's `collection.json` maps each values file and cartridge run to its existing B-roll
run. Its `broll_run_ids` lists can include additional clips later. Generating a
cartridge does not generate or modify any B-roll.

```text
runs/<run_id>/
  cartridge.json
  cartridge_prompt.txt
  cartridge.png
  run.log
```

`cartridge.json` records structured values, the compiled prompt and template
checksum, image settings, stage status, request ID, usage, and artifact checksums.
The updated supplied wording is preserved in `prompts/cartridge/v2.txt`.
These are PNG cutouts of complete cartridges for later compositing, not Blender
meshes or flat label textures. Use `render` to composite them into the reel intro.

The original full-scene template and values remain available. To use them, select
`--template-version v1 --background opaque` with a values file directly under
`examples/cartridges/`, such as `examples/cartridges/deep-end.json`. Version 1 uses
`LABEL_SCENE_DESCRIPTION`; version 2 requires `HINT_SCENE_DESCRIPTION` and a
transparent background. The two scene fields are mutually exclusive. Old saved
runs without a background setting retain their original opaque interpretation.

```sh
save-reel resume-cartridge runs/deep-end-cartridge-v2
```

Resume reuses and verifies a saved image. A request that failed without a saved
image is not automatically repeated because its billing outcome may be unknown.
A `.cartridge.lock` directory prevents simultaneous execution of one run. If a
process is killed, confirm it has stopped before removing that lock.

## Generate the real DEEP END B-roll

Install the optional API dependencies:

```sh
python -m pip install -e '.[media]'
```

Set `OPENAI_API_KEY` and `MINIMAX_API_KEY` in your environment or in a local `.env`
file. `.env.example` lists the names; do not put actual keys into that example.
The real `.env` is excluded from Git. Existing environment variables take
precedence over `.env`. MiniMax requires a pay-as-you-go API key with H3 access.
Use `--env-file /path/to/.env` to load a different file.

From the repository root:

```sh
save-reel generate-broll \
  --values examples/deep-end.json \
  --motion-values examples/deep-end-motion.json
```

This command makes **paid API requests** for one game: first a
`gpt-image-2.5-sunburst` image, then a `MiniMax-H3` video. The default still is an
opaque 864×1536 PNG (exactly 9:16) at medium quality. The default video is five
seconds at 768P. H3 takes its aspect ratio from the first-frame image. You can
select `--image-model gpt-image-2.5-flare`, `--image-quality high`, `--duration 6`,
or `--resolution 2K`. Duration must be an integer from 4 through 15.

MiniMax can return quantized dimensions and a slightly different duration. The
first DEEP END run returned a 768×1344, 24 fps clip lasting 5.167 seconds from the
864×1536 still. This workflow saves the provider output without cropping,
resizing, or trimming; the separate `render` command performs exact 9:16 finishing.

New B-roll runs use `prompts/broll_still/v2.txt` and `prompts/broll_video/v2.txt`.
DEEP END now emphasizes a moonlit patch of luminous pool water, low mist, and
restrained old-game effects: soft bloom, glow halos, scrolling caustics, and sparse
glowing particles. Motion comes from `examples/deep-end-motion.json`: a slow,
gently wandering step along the deck while generally facing the same pool and
slides. The scene keeps its simple geometry and low-resolution materials.
Both fully compiled prompts are saved before generation.

Use `--still-template-version v1 --video-template-version v1` to select the
original template wording with your chosen values. Template versions can be
selected independently. Resuming a run uses its saved prompts and values.

The revised motion values request silence, since you will supply MP3 music later.
This is a prompt instruction, not an API guarantee: the documented
[H3 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)
has no audio-off parameter. Its published
[pricing](https://platform.minimax.io/docs/pricing/overview) lists video charges by
duration and resolution, with no separate generated-audio surcharge or silent
discount listed. The downloaded video may still contain audio. The `render`
command removes it and adds the countdown track. Mixing your supplied MP3 is a
future editing step.

```text
runs/<run_id>/
  broll.json
  broll_still_prompt.txt
  broll_video_prompt.txt
  broll_still.png
  broll_video.mp4
  run.log
```

`broll.json` records the values, motion settings, models, prompt/template
provenance, stage states, image request ID, MiniMax task ID, usage returned by the
providers, and artifact checksums. It has kind `broll_generation`, distinct from
a four-save reel's `manifest.json`. The original four-save requirement for
`create` is unchanged. The generated PNG is sent directly to MiniMax as a base64
first-frame image; no public image hosting is needed.

To generate only the image, add `--image-only`. To continue that run, or resume
polling/downloading after a timeout, use its saved directory:

```sh
save-reel resume-broll runs/deep-end-broll
```

Use the directory printed by your own run. A new `--run-id` cannot overwrite an
existing run. Resume verifies existing artifact checksums, reuses a saved image,
and polls the saved task ID instead of submitting another generation. The default
poll interval is ten seconds, with a fifteen-minute waiting limit; use
`--wait-timeout 1200` to wait longer. A timeout stops local waiting, not the remote
task. MiniMax's task query endpoint retains tasks for seven days.

Paid generation POSTs have automatic retries disabled. If a request fails or is
interrupted before its image/task ID can be saved, the run refuses to resubmit
automatically because the provider may have accepted it. Check the provider and
the run log before creating another run. A `.broll.lock` directory prevents
concurrent execution; if a process is forcibly killed, confirm it has stopped
before removing that lock and resuming. API keys and raw responses are not written
to the run state or logs, and the MiniMax key is never sent to the download CDN.

The integrations follow the official [OpenAI image-generation API](https://developers.openai.com/api/docs/guides/image-generation),
[MiniMax H3 creation API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create),
and [MiniMax task-query API](https://platform.minimax.io/docs/api-reference/video-generation-v2-query).

## Create a reel

```sh
save-reel create --theme "forgotten worlds" --run-id demo
```

The equivalent module command is `python -m save_reel create`. If `--run-id` is
omitted, a UTC timestamp and random suffix produce a new run ID. An existing ID
is rejected, including IDs belonging to failed runs. Use a new ID to retry.

By default, `runs/` is relative to the working directory, not the filesystem root.
Use `--runs-dir /path/to/runs` to choose another destination. The CLI prints the
manifest path to stdout and logs progress to stderr and `run.log`.

```text
runs/demo/
  manifest.json
  run.log
  saves/
    save_01/
      label_prompt.txt
      broll_still_prompt.txt
    save_02/
      label_prompt.txt
      broll_still_prompt.txt
    save_03/
      label_prompt.txt
      broll_still_prompt.txt
    save_04/
      label_prompt.txt
      broll_still_prompt.txt
```

The mock provider always uses the same four worlds. The theme appears in the reel
title and save summaries; it does not invent new environments. Your templates use
the explicit world details, so changing `--theme` alone does not change these
image prompts. Sample values currently live in `src/save_reel/providers/mock.py`.
The `create` command still uses those sample worlds. To preview one game's
supplied values independently, use the Python example below.
`create` defaults to the original `v1` templates; add `--broll-template-version v2`
to use the revised B-roll style for its four mock worlds.

## Preview DEEP END

Your supplied values are saved in `examples/deep-end.json`, with small additions
to the scene, mood, and composition for the moonlit focal point and dreamlike air.
The original generated run keeps its original inputs and compiled prompts.
The explanatory lines "Used only in the b-roll prompt." are excluded from the
values. Shot composition and key surfaces are used only by the B-roll template.

From the repository root, after installation:

```python
import json
from pathlib import Path

from save_reel.prompting import BrollStillPromptCompiler, LabelPromptCompiler
from save_reel.storage import RunStore

values = json.loads(Path("examples/deep-end.json").read_text(encoding="utf-8"))
prompts = {
    "label_prompt": LabelPromptCompiler().render(values),
    "broll_still_prompt": BrollStillPromptCompiler("v2").render(values),
}
store = RunStore.create(Path("runs"))
for name, prompt in prompts.items():
    store.write_text(f"{name}.txt", prompt.text)
store.write_text(
    "preview.json",
    json.dumps({
        "kind": "prompt_preview",
        "values": values,
        "prompts": {name: prompt.model_dump(mode="json") for name, prompt in prompts.items()},
    }, indent=2) + "\n",
    media_type="application/json",
)
print(store.run_dir)
```

This produces two text prompts and `preview.json` with the values, compiled text,
and template provenance. A single-game preview is separate from `create`, which
continues to require exactly four saves and writes a reel `manifest.json`. No
additional game concepts or cartridge properties are invented for the preview.

## Architecture

```text
prompts/
  __init__.py
  label/v1.txt
  cartridge/v1.txt  # Full cartridge render with structured shell design values
  cartridge/v2.txt  # Transparent cartridge cutout with a teaser label
  broll_still/v1.txt
  broll_still/v2.txt  # Dreamlike nostalgia and deliberate old-game effects
  broll_video/v1.txt
  broll_video/v2.txt  # Preserve those effects during gentle exploration
  environment/v1.txt  # Original template, retained for existing workflows
src/save_reel/
  models.py         # Creative schemas, reel state, stage status, artifact metadata
  providers/
    base.py         # ConceptProvider protocol
    mock.py         # Offline structured concepts
    media.py        # Image/video provider contracts
    openai_image.py # GPT Image 2.5 adapter
    minimax_video.py # MiniMax H3 V2 adapter
  prompting.py     # Strict template rendering and template provenance
  storage.py       # Run paths, JSON loading, atomic file replacement
  stages.py        # ReelStage protocol and prompt compilation stages
  pipeline.py      # Provider validation, ordered stage execution, checkpoints
  broll.py         # Single-game real generation and resume checkpoints
  cartridge.py     # Cartridge generation and resume checkpoints
  media_models.py  # B-roll/cartridge inputs, shared image settings, and state
  render_models.py # Ordered collections, editable timing/layout, and render state
  rendering.py     # FFmpeg composition, countdown audio, and output validation
  cartridge_cli.py # Image-only cartridge command setup
  media_cli.py     # Optional SDK setup and environment credentials
  cli.py           # Argument parsing and dependency assembly
  __main__.py      # python -m save_reel
tests/
```

The workflow is:

```text
ConceptRequest -> ConceptProvider -> ReelConcept -> Reel with four SaveGames
                                                    |
                                 LabelPromptStage + BrollStillPromptStage
                                                    |
                                       manifest.json + eight prompt files
```

`EnvironmentSpec`, `CartridgeSpec`, and `EffectsSpec` describe creative variables.
`SaveGameConcept` and `ReelConcept` are provider-facing schemas without workflow
metadata or final prompts. `SaveGame` and `Reel` add IDs, stage state, prompts, and
artifacts. Both concept and reel schemas enforce exactly four saves, and reels
require unique save IDs. Unknown fields and empty required text are rejected.

The manifest includes schema version `1.0`, the request, provider name, timestamps,
all creative variables, compiled prompt text, template version and SHA-256, stage
states, and artifact paths and checksums. Artifact paths are relative to the run
directory so runs can be moved. The JSON is validated again before each write.

Stages record `pending`, `running`, `completed`, or `failed` status. The pipeline
checkpoints at stage boundaries; the prompt stage also checkpoints each save.
Writes use temporary files and atomic replacement. If a stage raises an exception,
the manifest records the failure and retains already completed artifacts. A
provider failure before a valid four-save concept exists leaves a run log but no
manifest. Automatic resume/retry is not part of this milestone. A completed reel
means the configured stages completed; the default stages produce prompts only.

## JSON and Python usage

```python
from pathlib import Path

from save_reel.models import ConceptRequest, Reel
from save_reel.pipeline import CreatePipeline
from save_reel.providers import MockConceptProvider
from save_reel.storage import RunStore

reel = CreatePipeline(MockConceptProvider()).create(
    ConceptRequest(theme="quiet adventures"),
    runs_dir=Path("runs"),
)
encoded = reel.model_dump_json(indent=2)
decoded = Reel.model_validate_json(encoded)
assert decoded == reel

loaded = RunStore(Path("runs") / reel.run_id).load_manifest()
assert loaded == reel
```

## Versioned templates

AI generates **structured creative variables**, not complete final prompts.
Deterministic, version-controlled templates own instructions, composition, style,
and exclusions. The label and B-roll templates use the supplied `[VARIABLE]`
notation. Substitution is strict: unknown variables are errors, and inserted
values are never evaluated as template code or recursively expanded. Literal
dollar signs in these templates are preserved.

| Placeholder | Structured source | Used by |
| --- | --- | --- |
| `[GAME_TITLE]` | `save.title` | Both |
| `[WORLD_SCENE]` | `save.environment.world_scene` | Both |
| `[COLOUR_PALETTE]` | `save.environment.palette` | Both |
| `[MOOD]` | `save.environment.mood` | Both |
| `[SHOT_COMPOSITION]` | `save.environment.shot_composition` | B-roll |
| `[KEY_SURFACES]` | `save.environment.key_surfaces` | B-roll |

Palette and surface lists become comma-separated text. Scene, shot composition,
and surfaces are supplied explicitly by the mock provider. They remain optional
in the persisted model so original manifests can still load, but the compilers
require their corresponding values before producing a prompt. Image prompts use
`save.title` as the single title for both images. Cartridge properties remain
available separately for a future Blender stage.

Keep released templates unchanged. Add `prompts/label/v2.txt` for wording
changes, then select it while leaving the B-roll template at `v1`:

```sh
save-reel create --label-template-version v2 --run-id version-two
```

This example requires creating `v2.txt` first; only `v1` ships initially. A template
version has the form `v` followed by a positive integer. Use
`--broll-template-version` to select the B-roll version separately, or
`--template-version` to set the default for both. Templates are packaged as
`save_reel.prompt_templates`, so installed commands work from any directory.
To use templates directly from another directory (including local development):

```sh
save-reel create --prompts-dir ./prompts --template-version v1
```

The original `environment/v1.txt` and its `${variable}` syntax are unchanged.
`EnvironmentPromptCompiler` and `EnvironmentPromptStage` remain available for
explicit Python workflows and old manifests, but are no longer in the default
stage list. The default output is now named `broll_still_prompt.txt` and uses your
B-roll wording instead of the earlier generic environment template.

## Extending concept providers

Implement the `ConceptProvider` protocol: a `name` property and
`generate_concept(request: ConceptRequest) -> ReelConcept`. Inject the provider
into `CreatePipeline`; the pipeline and templates do not need to change.

For a future OpenAI provider:

1. Keep client setup, credentials, model choice, and API-specific errors in a new
   provider module. Make the API SDK an optional dependency.
2. Request structured output matching `ReelConcept.model_json_schema()` and
   validate the returned data with `ReelConcept.model_validate(...)` or
   `ReelConcept.model_validate_json(...)`.
3. Return creative variables only. Do not ask the model to write final prompts or
   choose paths, IDs, workflow state, or template versions.
4. Add provider selection in `cli.py`. The current CLI accepts only `--provider mock`.

## Extending the workflow

Implement `ReelStage` with a unique `name` and
`run(reel: Reel, store: RunStore, logger: logging.Logger) -> None`. A stage updates
reel/save state, writes files under the run directory, and records output metadata
in `save.artifacts`. Raise an exception on failure so the pipeline records it.
Stage names must be unique; `concept_generation` is reserved.

Pass an ordered sequence through `CreatePipeline(provider, stages=(...))`.
Providing `stages` replaces the default sequence, so include
`LabelPromptStage(LabelPromptCompiler())` and
`BrollStillPromptStage(BrollStillPromptCompiler())` before consumers of their
output. Dependencies are explicit through ordering and recorded artifacts; this
milestone does not include a scheduler or dependency graph.

The real B-roll workflow uses `ImageProvider` and `VideoProvider` protocols in
`providers/media.py`. Their adapters can also be injected into future Reel stages.
Image generation consumes the deterministic B-roll still prompt; MiniMax consumes
that image and a separate deterministic motion prompt.
Blender can consume the generated label images and `CartridgeSpec`, and
`ReelRenderer` already consumes recorded cartridge and video artifacts through
FFmpeg. A future narration provider can generate audio using the saved narration
text and introduction timing, then extend the assembly audio mix. Keep API clients
and process execution inside their adapters/stages. Add their own versioned
templates where needed. None of these integrations needs to change the concept
provider contract or the stage runner.

## Tests

```sh
python -m pip install -e '.[dev,media]'
python -m pytest
python -m ruff check .
```

Tests cover schema constraints, JSON round-trips, deterministic strict rendering,
run isolation, prompt and manifest output, stage failures, provider/stage
injection, original-manifest compatibility, supplied prompt wording, independent
template versions, and both CLI entry points. All run artifacts created by tests live in
temporary directories.

Media tests use fake providers and mock HTTP transports, including real PNG
validation, V2 payloads, credential-free CDN downloads, failure checkpoints,
resume behavior, and prevention of duplicate submissions. They make no real API
calls. They are skipped when optional media dependencies are absent.

`tests/test_rendering.py` includes a small real FFmpeg render using synthetic
transparent PNGs and coloured clips. It checks compositing, bobbing, reveal order,
timing, countdown sound, removal of source audio, and recorded failures. Those
integration checks skip when FFmpeg, ffprobe, or a suitable font is unavailable;
render schema/timeline tests still run. For focused local verification:

```sh
python -m pytest tests/test_rendering.py tests/test_cli.py
```
