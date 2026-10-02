# save-reel

A generator for short-form **Choose Your Save** videos. The story workflow builds
four practical survival worlds, prepares structured visual variables and deterministic
prompts, and saves a JSON manifest. The original `create` workflow still prepares four
mock saves and their label/B-roll prompts. Both mock workflows run without API keys.

Optional real workflows generate full cartridge renders with GPT Image 2.5,
or generate B-roll stills and animate them with MiniMax H3. The local `render`
command combines saved cartridges and B-roll into a vertical MP4 with FFmpeg.
The `narrate` command adds ElevenLabs speech and timed subtitles from a manually
written script. Separate flat label image generation, Blender, orchestration,
and social publishing are not implemented.

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

## Generate survival stories without media

The default story configuration is **v3**, with two phases: WORLD SIMULATION and
NARRATION. World design stays on `gpt-6-luna`; the final writer uses `gpt-6.1-sol`:

- World design, candidate critiques and simulation approval: `reasoning.effort=high`.
- Narration description, writing and selection: `reasoning.effort=medium`.

Set `world_reasoning_effort` and `narration_reasoning_effort` in
`examples/story-settings.json` to change them. Configure the writer independently with
`narration_model`. [Sol supports medium reasoning](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
Optional `candidate_reasoning_effort` overrides only the initial candidate-writing
stage; for example, `"medium"` can reduce generation time while simulations and
reviews retain High reasoning. Omit it to inherit `world_reasoning_effort`.

Story commands never generate images, video, speech or FFmpeg output; existing clips
and visual templates stay intact.

For actual writing review, install the text extra and set `OPENAI_API_KEY` in the local,
ignored `.env` or environment:

```sh
python -m pip install -e '.[story]'

# Treat existing accepted v1/v2/v3 stories as occupied creative territory.
save-reel import-story-history runs --history-dir runs/.story-history

# Paid text only: generate 20 episodes for writing review.
save-reel generate-concepts --provider openai --count 20 --run-id survival-v3 \
  --settings examples/story-settings.json
save-reel review-concepts runs/survival-v3-001
```

Use `--model` or `SAVE_REEL_STORY_MODEL` to change the concept/world model. Precedence is CLI,
environment/`.env`, settings file, default. `--env-file` selects the credentials file.
Model access failures are reported; there is no silent substitution. A normal successful
episode uses 37 text calls: four candidate sets, four critiques, one reel critique,
then seven calls per save (simulation, simulation review, concise description, three
separate narration drafts, narration selection). Validation and quality replacement can add calls within
the configured limits. No paid calls are required by the automated tests.

For one offline plumbing check:

```sh
save-reel generate-concepts --provider mock --run-id fixture-check \
  --history-dir runs/fixture-history
```

**Mock output is not Luna output and is not a writing-quality assessment.** The earlier
20-run batches used the v1 mock, which combined fixed settings, anomalies, rules, costs,
palettes and suffixes. The v2/v3 mock serves twelve complete authored fixture worlds. It does
not invent unlimited worlds or pretend to follow creative constraints. Repeating it
against the same history eventually exhausts the fixtures and fails the novelty gates.
Use `--provider openai` for a 20-episode creative review. The CLI retains its offline-safe
mock default and prominently labels fixture output.

### World simulation, then narration

1. `story_seeds.py` samples only **broad constraints** from `prompts/world_seeds/v2.json`:
   role, setting family, surface/deeper emotion, general resource pressure, severity,
   era, broad time and weather. It contains no final setting, anomaly, danger, rule,
   inhabitants, cost, hook, palette or title.
2. Luna invents three complete alternatives per save. `WorldConcept` includes the
   specific setting, connected resource/danger/rule/consequence/cost system, inhabitants,
   mystery, visual hook and palette. Five required causal explanations connect the
   environment to danger, protection, failure, permanent cost and choice appeal.
3. `story_novelty.py` rejects recent exact titles, settings, anomalies, rules and costs;
   generic overused title suffixes/phrases/tokens; and excessive exact palette combinations.
   It compares compact setting/danger/rule/cost/anomaly/inhabitant signatures locally.
4. A separate structured critique scores novelty, causal coherence, survival specificity,
   visual hook, choice appeal, mystery, non-poetic writing and overall quality. It also
   reports its nearest recent concept and semantic similarity. Both local checks and
   the critic must allow the candidate; a high critic score cannot override a hard reuse
   failure. A rejected set triggers fresh candidates, not pressure to approve the old set.
5. Each save's strongest eligible candidate enters a four-save review. Deterministic checks
   block duplicate or cosmetically changed settings, threats, central mechanics and palettes.
   The reel critic assesses differences across environments, resources, rules, inhabitants,
   costs, emotions and palettes. A weak/redundant choice is replaced and the set rechecked.
6. **WORLD SIMULATION (High):** expand the selected concept into structured environment,
   resource system, food/water, shelter, inhabitants, threat, warnings, critical rule,
   consequence, escape conditions, daily routine, permanent cost and causal connections.
   No spoken lines are generated. Preserve the selected survival mechanics. Existing
   visual-variable schemas are populated alongside the spec; visual templates are unchanged.
7. A High-effort simulation review checks the setting/threat/rule/consequence chain and
   consistency of supplies, shelter and routine. Narration cannot start until it passes.
8. **NARRATION (GPT-6.1 Sol, Medium):** first condense the approved world into at most
   180 words of background. The final writer receives only that description, the title,
   word range and the user's example script. The supplied prompt asks for one
   45–70 word narration in short sentences and common words, around grade 5–6 level:
   a normal day, one practical task and one strange or dangerous rule. It excludes image
   descriptions, checklists, poetic lines and forced twists. Three independent calls
   produce alternatives; each successful draft is cached before the next call. The only
   writer output field is the paragraph. No survival schema, history, fact-count quotas
   or citation metadata enters the writer. The example guides voice, not world mechanics.
9. A separate Sol Medium pass rates naturalness and checks grounding against the concise
   description. The most natural acceptable paragraph wins; stable IDs break ties.
   If all fail, rewrite narration with compact feedback. Worlds, novelty decisions and
   visual exports remain unchanged. Description/candidates/review are cached separately.

The voice should calmly describe a bizarre place it knows well. Sentences connect
naturally, without covering every survival category or making every sentence ominous.
Sensory detail does not need a survival justification. There is no practical-line quota,
deletion test, danger ratio or obligatory twist. Paragraph length and structure
are validated locally; grounding and literary judgments use the independent model review,
not a claim of mathematical entailment. Live writing still needs review.

Both new phases checkpoint drafts and reviews independently. Rejected outputs and
reasons remain in `world_attempts` / `travelogue.attempts`. Each phase gets at most
`validation_retries + 1` quality attempts, in addition to the existing bounded schema
validation retries. A failing critic is not repeatedly queried to obtain approval.

The four roles remain attractive, nostalgic, mysterious and ominous, without fixed
inhabitant types, danger banks or a mandatory severity pattern. Configure the new word
range under `travelogue` in `examples/story-settings.json`; `narration` retains the old
briefing policy for legacy runs. Paragraphs have stored word counts and estimated seconds.
`narration_model` defaults to `gpt-6.1-sol`; `narration_reasoning_effort` defaults to
`medium`. The separate concept/world `model` remains `gpt-6-luna`.
Actual timing still comes from TTS; no audio timing or reel-assembly behavior is changed.

### Creative history and acceptance settings

`examples/story-settings.json` documents the knobs. Under `novelty`, defaults are:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `recent_exact_history` | 50 | Exact reuse and palette-frequency window |
| `recent_semantic_history` | 100 | Concept-signature comparison window |
| `title_history_window` | 50 | Title-token/phrase/suffix frequency window |
| `title_suffix_limit` | 1 | Maximum occurrences before rejecting another suffix |
| `title_token_limit` | 3 | Maximum occurrences before rejecting another title word |
| `title_phrase_limit` | 1 | Limit for repeated two-word title phrases |
| `palette_limit` | 2 | Limit for the same color set, independent of ordering |
| `similarity_threshold` | 0.78 | Reject at or above local/critic semantic similarity |
| `minimum_novelty` | 0.70 | Required critic novelty score |
| `minimum_coherence` | 0.75 | Required critic causal-coherence score |
| `minimum_overall` | 0.72 | Required critic overall score |
| `minimum_reel_diversity` | 0.65 | Minimum for every reel-diversity dimension |
| `max_candidate_rounds` | 3 | Candidate-set budget per save, including the first set |
| `max_reel_replacements` | 8 | Maximum replacements while repairing the four-save set |
| `summary_set_limit` | 12 | Maximum entries in each compact history frequency set |

These are recent-window limits, not permanent vocabulary bans. Setting the history
windows to zero disables historical checks, while within-reel checks remain active.
The old `recent_history_count` option applies only to v1.

History consists of deduplicated JSON records under `runs/.story-history`. The model
receives bounded title/frequency sets and short one-line concept signatures, **never
full old narration**. History appears once in candidate/critic prompts as occupied
territory, not inspiration. Simulation/narration expansion does not receive negative history.
Use `--history-dir` for a shared index or an isolated experiment. Local signature matching
uses small word/phrase normalizations; the critic handles broader paraphrases. Neither
is a mathematical guarantee of originality, and there is no vector database.

`import-story-history` accepts run roots, individual run directories, `story.json`, or
`manifest.json`. It merges completed v1/v2/v3 stories, skips unfinished runs, upgrades old
compact metadata, and is idempotent. It neither regenerates content nor rewrites source
runs. The original v1 templates/catalogue/mock remain solely for explicit legacy runs
and cache compatibility; v2/v3 production generation does not read their creative banks.

### Inspect, resume and regenerate

Each run keeps the existing `manifest.json` plus a typed `story.json`. The latter stores
broad seeds, all candidate rounds, scores, nearest matches, rejection reasons, selections,
reel reviews, briefs, narration, visual variables, settings, prompt snapshots and provenance.
`story_review.txt` shows the same development decisions in readable form, including failed
runs. Exact requests/responses, including the stage's reasoning effort, are checkpointed
under `story_requests/`. Each save also exports `world_spec.json`, `world_review.json`,
`narration_candidates.json`, `narration_selection.json` and `narration_travelogue.json`.
The readable review shows all paragraphs, scores, rejection reasons and the selection.
Legacy briefings retain `narration_grounding.json` and `narration_review.json`.

Per-save `cartridge_values.json`, `broll_values.json`, `motion_values.json` and compiled
prompts retain the existing downstream contracts. The normal Reel manifest references
story state by checksum and records template/provider/model provenance. No existing
cartridge/b-roll templates, UI, rendering, narration or MiniMax code is redesigned.

```sh
save-reel resume-concepts runs/survival-v3-001
save-reel regenerate-concepts runs/survival-v3-001 --scope save \
  --save-id save_03 --run-id revised-world
save-reel regenerate-concepts runs/survival-v3-001 --scope candidates \
  --save-id save_03 --run-id new-alternatives
save-reel regenerate-concepts runs/survival-v3-001 --scope narration \
  --save-id save_03 --run-id revised-words
# Apply the new style to a previously approved v3 briefing world, changing just one save.
save-reel regenerate-concepts runs/survival-v3-001 --scope narration \
  --save-id save_03 --narration-style sol --run-id sol-words
save-reel regenerate-concepts runs/survival-v3-001 --scope reel --run-id fresh-reel
```

Regeneration forks the source run. Unchanged saves remain frozen; narration-only changes
preserve the world and its approved set. Resume reuses successful stages and responses.
A request interrupted before its response is saved may need a new text call on explicit
resume; remote idempotency is not claimed. A run lock prevents concurrent generation.
A batch stops on failure, leaving a review and checkpoints; resume or regenerate that
run, then start remaining episodes with a fresh prefix. `--quiet` prints review paths.

Existing v1/v2/v3 runs load and resume using their saved rules and request behavior.
New v3 runs default to the Sol writer. Narration-only regeneration preserves the saved style;
use `--narration-style sol` to explicitly upgrade one approved v3 world. That flag
is valid only with `--scope narration`. Its other three saves, world simulation, novelty
selection and visual variables stay frozen. Resume never silently upgrades cached prose.
A procedural `--seed` reproduces broad constraints given the same history; cached text
is the source of exact model-output reproducibility.

Provider adapters still implement `generate(stage, prompt, context, response_type)`.
The OpenAI adapter continues to use [Responses structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
with Pydantic. World instructions remain in `prompts/story_*/v3.txt`. Narration is versioned
separately in `prompts/story_narration_description/v3.txt`,
`prompts/story_narration_prose_candidate/v3.txt`, and
`prompts/story_narration_prose_selection/v3.txt`. The candidate template preserves the
user's supplied writing prompt and example. Old v1/v2 narration prompts remain for cached
runs. Simple prose contracts live in
`story_prose_models.py`; shared narration state and selection live in
`story_narration_models.py` and `story_travelogue.py`; the existing bridge remains in
`story_simulation.py`. Novelty checks in `story_novelty.py`,
history storage and candidate replacement in `story_development.py` keep their behavior. Live writing
quality should be reviewed using the OpenAI command above; fixture tests prove workflow
behavior and rejection logic rather than literary quality.

## Render the reel locally

Requires FFmpeg and ffprobe on `PATH`, with the `libx264` encoder and `drawtext`
filter available. Rendering is verified with FFmpeg 9.0.1. The console opener
bundles Oxanium for display text and IBM Plex Mono for system labels. B-roll
titles/story captions retain the existing font; `--font-file /path/to/font.ttf`
controls that font. No API keys or optional
media Python dependencies are required for rendering existing assets.

With the four cartridge and B-roll runs already generated:

```sh
save-reel render \
  --collection examples/cartridges/v2/collection.json \
  --settings examples/reel-console.json \
  --run-id reel-console-base
```

The command prints `runs/reel-console-base/reel.mp4`. Output remains
1080×1920, 24 fps, H.264 with AAC audio. This intermediate render reserves a
four-second starfield intro; `narrate` then finalizes its length from speech.
Before narration, its 29-second timeline is:

| Time | Content |
| --- | --- |
| 0–4 seconds | Near-black starfield reserved for the spoken intro |
| 4–9 seconds | Cartridge grid appears, countdown from 5 to 1, one tone per second |
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

Edit `examples/reel-console.json` to change the countdown, clip duration,
bobbing, opener styling, sound volume, or output size.
Durations must fit whole frames and dimensions must be even and exactly 9:16.
The introduction timing is stored explicitly for the separate ElevenLabs
`narrate` command described below. Additional
B-roll run IDs in a game's `broll_run_ids` list play consecutively before the next
game, each for `clip_seconds`.

Set `loop_short_clips` to `true` to repeat existing footage locally until
`clip_seconds` is reached, without generating new video. Titles appear once per
reveal, and source clip audio remains muted. The default is `false`, which keeps
the existing error when a source clip is too short.

The `opener` object centralizes UI colours, opacity, dimensions, coordinates,
spacing, star density/motion, and typography. Defaults live in `OpenerStyle` in
`src/save_reel/opener_style.py`; all positions scale from a 1080×1920 design space.
Set `opener.display_font_file` and `opener.system_font_file` to local font paths
to override the bundled fonts. Relative paths resolve from the working directory.
Fonts and generated starfield/halo textures are copied into each run and checksummed.
The bundled font licenses and sources are in `src/save_reel/assets/fonts/`.

The opener contains only narration captions, the cartridge slots, SAVE 01–04,
MAKE YOUR CHOICE, and the countdown. There is no static heading, explanatory
paragraph, or slogan. Stars drift gently; neutral low-opacity halos and small
corner marks separate the independently floating cartridges from the background.
Old `heading`, `intro_lines`, and `background_color` settings still deserialize
for compatibility, but no longer control the opener. B-roll lower thirds and
story-caption styling are unchanged.

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
  ui/...              # Display/system fonts, deterministic starfield and halo textures
  command_*.json       # Exact FFmpeg argument lists
  ffmpeg_*.log
  run.log
```

The run is self-contained and retains its inputs for future editing. Failed
renders record the failed stage and retain diagnostic logs; start a new run after
fixing the cause. `render.json` describes assembly and remains separate from the
creative `manifest.json` produced by `create`. Rendering calls no paid providers.

## Add narration and subtitles

Install the optional speech dependencies (also included in `.[media]`):

```sh
python -m pip install -e '.[speech]'
```

Add `ELEVENLABS_API_KEY` to your local `.env`. The key needs Text to Speech access;
automatic voice selection also needs permission to read Voices. Optionally set
`ELEVENLABS_VOICE_ID` or pass `--voice-id`. Otherwise, the command selects an
available stock voice, preferring George. It does not clone or add voices. As with
the other API commands, existing environment variables take precedence over `.env`;
`--env-file` chooses another local file. Never commit credentials.

```sh
save-reel narrate \
  --render-run runs/reel-console-base \
  --script examples/narration-console.json \
  --run-id reel-console
```

Without cached speech, this makes **five paid ElevenLabs requests**: one
introduction and one reveal line per game. The default model is
`eleven_multilingual_v2`, with MP3 audio and
character timestamps from the
[speech-with-timing endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps).
`--model` selects another compatible TTS model; `--speed` controls generation
speed from 0.7 to 1.2. Voice selection uses the
[voice-list endpoint](https://elevenlabs.io/docs/api-reference/voices/search).

The supplied script is a manual first pass: DEEP END's unreachable exit, SUNROOM
LINE's safe journey, VELVET ORCHARD's stolen memories, and SIGNAL 03's unresolved
identity. Edit that JSON to change the narration. Its four titles and order must
match the source render. No LLM or orchestrator writes or changes the script.

For console openers, the intro must use the exact script in the supplied example:
"You have died. You must pick a game cartridge to be reincarnated into."
Two complete captions appear only during their corresponding spoken sentences:
"You have died" and "You must pick a game cartridge to be reincarnated into".
Neither appears during the gap between sentences. There are no cartridges yet.
After the second caption ends, the next video frame reveals the cartridge grid
and starts the five-second countdown. The intro plays at its generated pace;
the final duration follows its speech timestamps instead of a guessed pause.
The new timings are recorded in `narration.json` as `effective_intro_seconds`
and `timeline`. The source render's timeline remains intact.

To reuse existing recordings during a UI revision, add:

```sh
--voice-id JBFqnCBsd6RMkjVDRZzb --reuse-speech-run runs/reel-console-v1
```

The voice, model, and generation settings must match. Each cue with identical
text is copied and checksum-verified; only changed lines call ElevenLabs. The
initial console redesign reused all four story recordings and generated only
the revised introduction. Later typography edits reused all five recordings.
Use a fresh output run ID for each revision.

FFmpeg places the four stories in their B-roll windows, shifts the countdown
tones to follow the intro, and leaves the countdown speech-free. The voice track
is normalized and mixed with those tones. When a story
line is slightly too long, FFmpeg increases tempo without changing pitch, up to
1.35×; it fails with an actionable error if a line still will not fit. Leading
and trailing silence are trimmed using the returned timestamps. Speech is never
silently truncated to force it into a scene.

Short phrase subtitles follow those same timestamps and tempo adjustments. They
are burned into the image using FFmpeg `drawtext` and also saved as `subtitles.srt`.
Literal text files keep punctuation out of filter syntax. Pass `--no-subtitles`
to omit B-roll story captions; the two console intro captions and SRT remain.
Legacy render runs without console assets still use their saved fixed timing,
and preserve the video stream without re-encoding when subtitles are disabled.
The original render stays intact. Narration output is a separate run:

```text
runs/<narration_run_id>/
  reel.mp4              # Narrated reel, with subtitles by default
  narration.json        # Source render, script, voice settings, stages, checksums
  script.json
  source.mp4            # Copy of the original reel and its countdown audio
  opener.mp4            # Console opener rebuilt from narration timestamps
  countdown.wav         # Countdown tones on the final timeline
  ui/...                # Copied fonts and deterministic textures
  speech/intro.mp3      # Original generated speech; also save_01 through save_04
  speech/intro.json     # Character timestamps, request ID, reported character cost
  narration.wav         # Voice track positioned on the complete reel timeline
  speech_plan.json      # Trim/tempo/offset decisions and caption times
  subtitles.srt
  text/subtitle_*.txt
  voice_filter.txt
  mix_filter.txt
  font.ttf
  probe.json
  command_*.json
  ffmpeg_*.log
  run.log
```

```sh
save-reel resume-narration runs/reel-console
```

Resume verifies checksums and reuses saved speech. A completed run returns its
existing video. If local FFmpeg assembly failed after speech was saved, resume
retries only that local work and needs no API key. Paid POSTs are not automatically
retried: an attempted request without a saved result requires checking ElevenLabs
history before starting a new run. A `.narration.lock` prevents concurrent work;
if a process was killed, confirm it stopped before removing that lock.

This is a manual narration/assembly step. Music mixing and automatic orchestration
remain future work. The revised console output is `runs/reel-console-v2/reel.mp4`.

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
    speech.py       # SpeechProvider contract and generated speech result
    elevenlabs_speech.py # ElevenLabs voice discovery and speech with timestamps
  prompting.py     # Strict template rendering and template provenance
  storage.py       # Run paths, JSON loading, atomic file replacement
  stages.py        # ReelStage protocol and prompt compilation stages
  pipeline.py      # Provider validation, ordered stage execution, checkpoints
  broll.py         # Single-game real generation and resume checkpoints
  cartridge.py     # Cartridge generation and resume checkpoints
  media_models.py  # B-roll/cartridge inputs, shared image settings, and state
  render_models.py # Ordered collections, editable timing/layout, and render state
  rendering.py     # FFmpeg composition, countdown audio, and output validation
  opener_style.py  # Shared console typography, colours, layout and opacity tokens
  opener.py        # Offline starfield/halo assets, cartridge UI, timed intro captions
  ffmpeg_text.py   # Shared literal-text rendering; existing B-roll appearance preserved
  narration_models.py # Manual scripts, voice settings, character alignment, state
  narration.py     # Checkpointed speech generation, timed captions, and FFmpeg mix
  narration_cli.py # Optional speech dependency and credential setup
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
FFmpeg. `SpeechProvider` supplies audio plus character timing; `NarrationPipeline`
places that speech into the rendered scene windows and creates captions. Keep API clients
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

`tests/test_narration.py` checks ElevenLabs requests with mock HTTP, safe failures,
alignment validation, tempo-adjusted subtitle times, and cached resume without
duplicate requests. Its real local FFmpeg test verifies speech in each scene,
preserved countdown audio, and burned captions. No test makes paid API calls:

```sh
python -m pytest tests/test_narration.py tests/test_cli.py
```
