"use strict";

const $ = (id) => document.getElementById(id);
const h = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let token, draft, drafts = [], jobs = [], credentials = {}, defaults, runs = [];
let tab = "workspace", dirty = false, selectedPrompt = "story_narration_prose_candidate/v3.txt";
let selectedJob = null, lastActive = null, runSearch = "", runKind = "finished", polling = false;
let mediaSave = "", mediaBeat = "", forceMedia = false, allPrompts = false;
let pendingAdvanced = null;
let storyMode = "existing", presets = [], revisions = [], runStatus = "all", runDate = "";

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type":"application/json", "X-Studio-Token":token}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Request failed");
  return value;
}
function notice(message, error = false) { $("notice").textContent = message; $("notice").className = `notice${error ? " error" : ""}`; $("notice").hidden = !message; }
function active() { return jobs.find(j => ["running","queued"].includes(j.status)); }
function locked() { return active()?.draft_id === draft?.draft_id; }
function get(path) { return path.split(".").reduce((value, key) => value?.[key], draft); }
function set(path, value) {
  const keys = path.split("."), last = keys.pop();
  keys.reduce((object,key) => object[key], draft)[last] = value;
  if (/^games\.\d+\.title$/.test(path)) {
    const game = draft.games[Number(keys[1])]; game.cartridge.title = value; game.environment.game_title = value;
  }
  dirty = true; header();
}
function header() {
  $("draft-name").textContent = draft?.name || "No draft";
  $("save-state").textContent = locked() ? "Generation in progress" : dirty ? "Unsaved changes" : "Saved locally";
  $("save").disabled = !dirty || locked(); $("duplicate").disabled = !draft;
  $("job-dot").hidden = !active();
  $("running").hidden = !active();
  if (active()) $("running").innerHTML = `<div class="running-progress"><strong>${h(active().name)}</strong> · ${h(active().phase)}${progressBar(active())}</div><button class="button quiet" data-tab="jobs">View activity</button>`;
}
function draftMenu() {
  $("draft-select").innerHTML = drafts.map(d => `<option value="${h(d.draft_id)}" ${d.draft_id === draft?.draft_id ? "selected" : ""}>${h(d.name)}</option>`).join("");
}
async function save(silent = false) {
  if (!draft || locked()) return;
  if(pendingAdvanced!==null) await applyAdvanced();
  draft = await api("/api/save", draft); dirty = false;
  drafts = await api("/api/drafts"); revisions = await api(`/api/revisions/${draft.draft_id}`); draftMenu(); header();
  if (!silent) notice("Draft saved. New jobs will use these prompts and settings.");
}
function canLeave() { return !dirty || window.confirm("Discard unsaved changes to this draft?"); }
async function loadDraft(id) { draft = await api(`/api/drafts/${encodeURIComponent(id)}`); dirty = false; pendingAdvanced=null; storyMode=draft.games.length?"existing":"new"; revisions=await api(`/api/revisions/${draft.draft_id}`); draftMenu(); render(); }
async function newDraft(copy = false) {
  if (copy && dirty) await save(true);
  if (!copy && !canLeave()) return;
  draft = await api("/api/drafts", copy ? {copy_from:draft.draft_id,name:`${draft.name} — copy`} : {});
  if(!copy) { draft.provider="openai"; draft=await api("/api/save",draft); }
  storyMode=draft.games.length?"existing":"new"; revisions=await api(`/api/revisions/${draft.draft_id}`);
  dirty = false; pendingAdvanced=null; drafts = await api("/api/drafts"); draftMenu(); tab = "workspace"; render();
}
function input(path, label, options = {}) {
  const id = `field-${path.replaceAll(".","-")}`, value = get(path);
  let control;
  if (options.choices) control = `<select id="${id}" data-path="${path}">${options.choices.map(c => {
    const [v,l] = Array.isArray(c) ? c : [c,c]; return `<option value="${h(v)}" ${String(value ?? "") === String(v) ? "selected" : ""}>${h(l)}</option>`;
  }).join("")}</select>`;
  else if (options.type === "checkbox") control = `<label class="check" for="${id}"><input id="${id}" type="checkbox" data-path="${path}" ${value ? "checked" : ""}>${h(label)}</label>`;
  else if (options.area) control = `<textarea id="${id}" data-path="${path}" ${options.code ? 'class="json-editor" spellcheck="false"' : ""}>${h(value)}</textarea>`;
  else control = `<input id="${id}" data-path="${path}" type="${options.type || "text"}" ${options.type === "number" ? 'step="any"' : ""} value="${h(value)}">`;
  return `<div class="field ${options.full ? "full" : ""}">${options.type === "checkbox" ? "" : `<label for="${id}">${h(label)}</label>`}${control}${options.hint ? `<p class="hint">${h(options.hint)}</p>` : ""}</div>`;
}
function heading(title, description, extra = "") { return `<div class="page-heading"><div><span class="eyebrow">CHOOSE YOUR SAVE</span><h1>${title}</h1><p>${description}</p></div>${extra}</div>`; }
function stageButton(action, label, disabled = false) { return `<button class="button" data-action="${action}" ${active() || disabled ? "disabled" : ""}>${label}</button>`; }
const descriptions = {
  stories:"Generate four worlds and narration. OpenAI mode uses paid text calls; mock mode stays offline.",
  scripts:"Rewrite narration from the saved worlds with the configured OpenAI writer. Images and clips stay saved.",
  cartridges:"Generate cartridge PNGs through OpenAI. Matching saved images are reused.",
  stills:"Generate B-roll stills through OpenAI. This does not call MiniMax.",
  videos:"Generate missing stills and MiniMax clips. Matching completed clips are reused.",
  render:"Assemble existing assets locally with FFmpeg, without speech generation.",
  narrate:"Render existing assets, then add ElevenLabs narration and subtitles. Matching voice recordings are reused.",
  full:"Generate missing stories and media, then render a narrated reel. Uses OpenAI, MiniMax and ElevenLabs."
};
function workspace() {
  const ready = draft.games.length === 4;
  const latest=jobs.find(j=>j.draft_id===draft.draft_id&&j.status==="completed"&&j.outputs.reel);
  return heading("Reel workspace", "Make a complete video, from four stories to a narrated MP4.",
    latest ? `<button class="button primary" data-watch="${h(latest.outputs.reel)}/reel.mp4">Watch latest reel ↗</button>` : "") +
    `<fieldset ${locked() ? "disabled" : ""}><div class="panel"><div class="fields">
      ${input("name","Reel name")}
      <div class="field"><label for="story-mode">Stories for this reel</label><select id="story-mode"><option value="existing" ${storyMode==="existing"?"selected":""} ${ready?"":"disabled"}>Use the four stories below</option><option value="new" ${storyMode==="new"?"selected":""}>Generate four new stories</option></select><p class="hint">Import a saved story from the library, or generate new worlds.</p></div>
      ${storyMode==="new"?input("theme","Story direction",{full:true,hint:"A brief for the world writer, such as ‘quiet coastal towns with strange working routines’. It is not spoken in the reel."}):""}
      </div><div class="action-note"><div><strong>Stories → cartridges → stills → clips → voice → finished reel</strong><p>Uses OpenAI, MiniMax and ElevenLabs. Matching saved assets are reused.<br>Four saves run in parallel within each stage. The final MP4 includes narration and your caption settings.</p></div>
      <button class="button primary" data-action="full" ${active()?"disabled":""}>Generate finished reel ↗</button></div></div></fieldset>
    <details class="panel"><summary>Advanced: run or regenerate a single stage</summary><p class="hint">These controls produce individual parts. Use Generate finished reel above for the complete video.</p>
      <fieldset ${locked()?"disabled":""}><div class="fields">${input("provider","Story-only test provider",{choices:[["openai","OpenAI · live stories"],["mock","Mock · offline fixtures"]],hint:"Only affects story generation. Finished reels with new stories always use OpenAI."})}</div></fieldset>
      <div class="pipeline">${stageButton("stories","Stories only")}${stageButton("scripts","Rewrite narration only",!ready || !draft.source_story)}${stageButton("cartridges","Cartridges",!ready)}${stageButton("stills","B-roll stills",!ready)}${stageButton("videos","B-roll clips",!ready)}</div>
      <div class="toolbar"><label for="media-save">Saves to update</label><select id="media-save"><option value="">All four saves</option>${[1,2,3,4].map(i=>`<option value="${i}" ${String(i)===mediaSave?"selected":""}>Save 0${i}</option>`).join("")}</select><label for="media-beat">B-roll to update</label><select id="media-beat">${[["","Both clips"],["1","B-roll 1"],["2","B-roll 2"]].map(([v,l])=>`<option value="${v}" ${mediaBeat===v?"selected":""}>${l}</option>`).join("")}</select><label class="check"><input type="checkbox" id="force-media" ${forceMedia?"checked":""}>Make a new version, even if a matching asset exists</label></div>
      <p class="hint">New versions use paid API calls. Regenerating clips reuses matching stills. Story-only generation replaces all four stories in this draft; duplicate it to keep a variant.</p>
      <div class="run-actions">${stageButton("render","Base video only · local",!ready)}${stageButton("narrate","Finish existing media + voice",!ready)}</div>
    </details>
    <div class="page-heading"><div><h2>Your four saves</h2><p>These stories and variables belong to this reel. Edit them before generating its assets.</p></div><button class="button quiet" id="preview-prompts" ${!ready?"disabled":""}>Preview compiled prompts</button></div>
    ${ready ? `<fieldset ${locked()?"disabled":""}><div class="games">${draft.games.map(gameCard).join("")}</div></fieldset>` : `<div class="empty"><div class="empty-number">01 / 02 / 03 / 04</div><h2>Ready for four new worlds</h2><p>Generate finished reel will write the stories and make the entire video.</p><button class="button" data-tab="library">Browse saved stories and reels</button></div>`}`;
}
function presetControls() {
  return `<fieldset ${locked()?"disabled":""}><details class="panel"><summary>Reusable presets & revision history</summary><p class="hint">Presets copy prompts and model/settings choices into any reel. Stories and assets stay with the reel. Every saved preset is an independent revision.</p>
    <div class="fields"><div class="field"><label for="preset-name">Name this preset revision</label><input id="preset-name" placeholder="For example: Quiet worlds · revision 2"><button class="button" id="save-preset">Save preset revision</button></div>
    <div class="field"><label for="preset-select">Saved preset revisions</label><select id="preset-select">${presets.length?presets.map(p=>`<option value="${h(p.id)}">${h(p.name)} · ${h(new Date(p.created_at).toLocaleString())}</option>`).join(""):'<option value="">No saved presets</option>'}</select><button class="button" id="apply-preset" ${presets.length?"":"disabled"}>Apply to this reel</button></div>
    <div class="field full"><label for="revision-select">Earlier saved settings in this reel</label><select id="revision-select">${revisions.length?revisions.map(r=>`<option value="${r.revision}">Revision ${r.revision} · ${h(new Date(r.created_at).toLocaleString())}</option>`).join(""):'<option value="">No earlier revisions</option>'}</select><button class="button" id="restore-revision" ${revisions.length?"":"disabled"}>${tab==="prompts"?"Restore this template":"Restore prompts and settings"}</button><p class="hint">Restoring creates a new draft revision. Completed runs and job snapshots keep their originals. Template v1/v2/v3 choices select a schema; saved revisions track your wording edits.</p></div></div></details></fieldset>`;
}
const labels = {world_scene:"World scene",colour_palette:"Colour palette",mood:"Mood",shot_composition:"Shot composition",key_surfaces:"Key surfaces",shell_material_color:"Shell material / colour",shape_language:"Shape language",molded_details:"Molded details",object_mood:"Object mood",hint_scene_description:"Label teaser",label_scene_description:"Label scene",camera_motion:"Camera movement",environment_motion:"Environmental movement",audio:"Generated audio direction"};
function gameCard(game,i) {
  const groups = [["environment","Shared B-roll world"],["cartridge","Cartridge variables"],["motion","Shared environmental movement"]];
  const words = game.narration.trim().split(/\s+/).filter(Boolean).length;
  return `<article class="panel game"><div class="game-top">${game.cartridge_run ? `<img class="game-art" src="/media/${h(game.cartridge_run)}/cartridge.png" alt="${h(game.title)} cartridge" loading="lazy">` : `<div class="game-index">0${i+1}</div>`}<div class="game-name"><label for="game-title-${i}" class="eyebrow">SAVE 0${i+1}</label><input id="game-title-${i}" data-path="games.${i}.title" value="${h(game.title)}"></div></div>
    <div class="game-body"><label for="narration-${i}">Narration <span class="word-count" id="words-${i}">${words} words</span></label><p class="hint">${game.narration_origin?.provider==="mock" ? "Offline fixture · real narration will be written before finishing this reel." : game.narration_origin?.provider==="openai" ? `Written with ${h(game.narration_origin.model)}` : "Editable script"}</p><textarea id="narration-${i}" class="narration" data-path="games.${i}.narration">${h(game.narration)}</textarea>
    ${input(`games.${i}.survivability_tier`,"Survivability tier",{choices:[["","Choose tier"],"best","good","risky","bad"],hint:"One of each per reel. This controls the short spoken verdict."})}${groups.map(([key,title])=>`<details><summary>${title}</summary>${Object.entries(game[key]).filter(([name,value])=>!['title','game_title',...(game.broll_beats?.length?['shot_composition','camera_motion']:[])].includes(name)&&value!==null).map(([name])=>input(`games.${i}.${key}.${name}`,labels[name]||name,{area:true})).join("")}</details>`).join("")}
    ${(game.broll_beats||[]).map((beat,j)=>`<details><summary>B-roll ${j+1} · ${h(beat.type)}</summary>${["description","shot_composition","camera_motion"].map(name=>input(`games.${i}.broll_beats.${j}.${name}`,labels[name]||"Visible area / subject",{area:true})).join("")}<div class="media-links">${beat.run_id?`<button class="button quiet" data-watch="${h(beat.run_id)}/broll_0${j+1}.mp4">View B-roll ${j+1}</button><button class="button quiet" data-watch="${h(beat.run_id)}/broll_0${j+1}_still.png">View still ${j+1}</button>`:""}</div></details>`).join("")}
    <div class="media-links">${!game.broll_beats?.length&&game.broll_run?`<button class="button quiet" data-watch="${h(game.broll_run)}/broll_video.mp4">View B-roll</button><button class="button quiet" data-watch="${h(game.broll_run)}/broll_still.png">View still</button>`:""}</div>${!game.broll_beats?.length?'<p class="hint">Legacy single-clip world. New stories include two distinct visual beats.</p>':""}</div></article>`;
}
const promptTitles = {story_narration_prose_candidate:"Narration writer",story_narration_description:"Concise world description",story_narration_prose_selection:"Narration selection",story_candidates:"World candidates",story_world_simulation:"World simulation",story_world_review:"World review",story_review:"Candidate review",story_reel_review:"Reel diversity review",story_rules:"World writing rules",story_tiers:"Survival outcome spread",story_broll_beats:"Two B-roll beats",cartridge:"Cartridge image",broll_still:"B-roll still",broll_video:"B-roll video"};
function promptName(path) { const [folder,version]=path.split("/"); return `${promptTitles[folder] || folder.replaceAll("_"," ")} · ${version.replace(".txt","")}`; }
function activePrompts() {
  const world=draft.story.prompt_version, narration=draft.story.travelogue?.prompt_version;
  const stages=["rules","candidates","review"];
  if(world!=="v1")stages.push("reel_review");
  stages.push(...(world==="v3"?["world_simulation","world_review"]:["brief","narration"]));
  if(world==="v3") {
    if(narration==="v3") stages.push("narration_description","narration_prose_candidate","narration_prose_selection");
    else if(narration==="v2") stages.push("narration_description","narration_prose_candidates","narration_prose_selection","narration_examples");
    else if(narration)stages.push("narration_candidates","narration_selection");
    else stages.push("narration","narration_review");
  }
  return new Set([...(draft.story.broll_prompt_version?[`story_broll_beats/${draft.story.broll_prompt_version}.txt`]:[]),...(draft.story.tier_prompt_version?[`story_tiers/${draft.story.tier_prompt_version}.txt`]:[]),...stages.map(stage=>`story_${stage}/${stage==="world_review"?(draft.story.world_review_prompt_version||world):stage.startsWith("narration_")&&narration?narration:world}.txt`),`cartridge/${draft.cartridge_version}.txt`,`broll_still/${draft.still_version}.txt`,`broll_video/${draft.video_version}.txt`]);
}
function promptEditor() {
  const activeNames=activePrompts();
  const names=Object.keys(draft.prompts).filter(name=>allPrompts||activeNames.has(name)).sort((a,b)=>a.localeCompare(b));
  if (!names.includes(selectedPrompt)) selectedPrompt=names.find(n=>n.includes("narration_prose_candidate/"))||names[0];
  return heading("Prompt editor", "Edit this reel’s prompt wording, or save a reusable preset for future reels.") + presetControls() +
    `<fieldset ${locked()?"disabled":""}><div class="panel"><div class="editor-toolbar"><label for="prompt-select">Template</label><select id="prompt-select">${names.map(n=>`<option value="${h(n)}" ${n===selectedPrompt?"selected":""}>${h(promptName(n))}</option>`).join("")}</select><button class="button quiet" id="reset-prompt">Reset this template</button></div>
    <label class="check"><input type="checkbox" id="all-prompts" ${allPrompts?"checked":""}>Show inactive versions too</label><div class="editor-meta"><span>${h(selectedPrompt)}</span><span>${activeNames.has(selectedPrompt)?"Active template":"Inactive version"} · draft copy</span></div><label class="eyebrow" for="prompt-body">PROMPT TEXT</label><textarea id="prompt-body" class="code-editor" spellcheck="false">${h(draft.prompts[selectedPrompt])}</textarea>
    <p class="hint">Keep placeholders such as [CONTEXT], [FEEDBACK] and [GAME_TITLE]. Model inputs fill them automatically. Choose active versions in Models & settings. Cached jobs keep their original prompts.</p></div></fieldset>`;
}
function settings() {
  const efforts=["low","medium","high","xhigh","max"], images=["gpt-image-2.5-sunburst","gpt-image-2.5-flare"], qualities=["low","medium","high","xhigh","max"];
  const sections = [
    ["Story & narration",`${input("parallel_saves","Parallel saves per stage",{choices:[1,2,3,4],hint:"Lower this if your provider rate limits simultaneous requests."})}${input("story.model","World model")}${input("story.narration_model","Narration model")}${input("story.world_reasoning_effort","World reasoning",{choices:efforts})}${input("story.world_review_reasoning_effort","World review reasoning",{choices:[["","Inherit world reasoning"],...efforts]})}${input("story.narration_reasoning_effort","Narration reasoning",{choices:efforts})}${input("story.candidate_reasoning_effort","Candidate reasoning",{choices:[["","Inherit world reasoning"],...efforts]})}${input("story.validation_retries","Validation retries",{type:"number"})}${input("story.candidates_per_save","Candidates per save",{type:"number"})}${input("seed","Random seed (optional)",{type:"number"})}${draft.story.travelogue ? input("story.travelogue.target_min_words","Minimum words",{type:"number"})+input("story.travelogue.target_max_words","Maximum words",{type:"number"}) : ""}`],
    ["Images & clips",`${input("cartridge.image_model","Cartridge model",{choices:images})}${input("cartridge.image_quality","Cartridge quality",{choices:qualities})}${input("broll.image_model","B-roll image model",{choices:images})}${input("broll.image_quality","B-roll image quality",{choices:qualities})}${input("broll.video_model","Video model",{choices:["MiniMax-H3","MiniMax-H3-Max"]})}${input("broll.clips_per_save","Clips per save",{choices:[1,2]})}${input("broll.duration","Generated clip length (seconds)",{type:"number"})}${input("broll.resolution","Video resolution",{choices:draft.broll.video_model==="MiniMax-H3-Max"?["480P","768P"]:["768P","2K"]})}`],
    ["Voice & assembly",`${input("speech_parallel_saves","Parallel voice recordings",{choices:[1,2,3,4],hint:"Separate ElevenLabs limit. Defaults to 2; increase only if your plan supports it."})}${input("speech.voice_id","ElevenLabs voice ID")}${input("speech.model_id","Speech model")}${input("speech.speed","Voice speed",{type:"number"})}${input("render.polish.enabled","Speech-timed cartridge reveals and varied footage",{type:"checkbox"})}${input("render.polish.title_seconds","Cartridge title beat (seconds)",{type:"number"})}${input("render.polish.max_slowdown","Maximum clip slowdown",{type:"number"})}${input("render.polish.allow_still_fallback","Use world still when footage runs out",{type:"checkbox"})}${input("render.polish.still_position","World still placement",{choices:["after","before"]})}${input("render.polish.still_zoom","Still zoom amount",{type:"number"})}${input("render.clip_seconds","Base preview duration per world (seconds)",{type:"number",hint:"In polished mode, final pacing follows actual speech length."})}${input("render.fps","Output frame rate",{choices:[24,30,60]})}${input("render.countdown_seconds","Countdown (seconds)",{type:"number"})}${input("speech.subtitles","Show subtitles",{type:"checkbox"})}${!draft.render.polish.enabled?input("render.loop_short_clips","Legacy: loop short B-roll clips",{type:"checkbox"}):""}${["best","good","risky","bad"].map(t=>input(`render.polish.tier_intros.${t}`,`${t[0].toUpperCase()+t.slice(1)} narrator intro`)).join("")}`],
    ["Active templates",`${input("story.prompt_version","World prompts",{choices:["v1","v2","v3"]})}${input("story.seed_catalog_version","Seed catalog",{choices:["v1","v2"]})}${input("story.world_review_prompt_version","World review prompt",{choices:[["","Inherit world prompts"],["v4","v4 — Game-world consistency"],["v3","v3 — Original review"]]})}${draft.story.travelogue?input("story.travelogue.prompt_version","Narration prompt",{choices:["v1","v2","v3"]}):""}${input("cartridge_version","Cartridge template",{choices:["v1","v2"]})}${input("still_version","Still template",{choices:["v1","v2"]})}${input("video_version","Video template",{choices:["v1","v2"]})}`]
  ];
  return heading("Models & settings", "Tune this reel, or reuse a preset. Existing videos keep their original settings.") + presetControls() +
    `<div class="credentials">${Object.entries(credentials).map(([name,ok])=>`<span class="pill ${ok?"":"failed"}">${h(name.replace("_API_KEY",""))} · ${ok?"key configured":"key missing"}</span>`).join("")}</div><p class="hint">Keys stay in your local .env file. Studio shows only whether they are configured.</p>
    <fieldset ${locked()?"disabled":""}><div class="settings-grid">${sections.map(([title,content])=>`<section class="panel"><h2>${title}</h2><div class="fields">${content}</div></section>`).join("")}</div>
    <div class="panel settings-advanced"><details><summary>Advanced story and render settings</summary><p class="hint">Edit the full settings, including novelty thresholds, UI colours and fonts. Apply JSON before editing the individual fields above. Saving also applies pending JSON.</p><label for="advanced-json">Story / render configuration</label><textarea id="advanced-json" class="json-editor" spellcheck="false">${h(pendingAdvanced??JSON.stringify({story:draft.story,render:draft.render,speech:draft.speech},null,2))}</textarea><button id="apply-json" class="button">Apply JSON to draft</button></details></div></fieldset>`;
}
function library() {
  const visible=runs.filter(r=>(runKind==="all"||(runKind==="finished"?r.finished:r.kind===runKind))&&(runStatus==="all"||r.status===runStatus)&&(!runDate||r.created_at.slice(0,10)>=runDate)&&`${r.name||""} ${r.run_id} ${r.titles.join(" ")}`.toLowerCase().includes(runSearch.toLowerCase()));
  return heading("Video library", "Finished reels first. Use the filters to inspect stories and individual assets.",`<button class="button quiet" id="refresh-library">Refresh</button>`) +
    `<div class="library-toolbar"><input id="run-search" aria-label="Search saved runs" placeholder="Search titles or run names…" value="${h(runSearch)}"><select id="run-kind" aria-label="Filter run type">${[["finished","Finished reels only"],["all","All assets and runs"],["narration","Narration runs (all stages)"],["render","Base renders"],["story","Stories"],["cartridge","Cartridges"],["broll","B-roll"]].map(([value,label])=>`<option value="${value}" ${runKind===value?"selected":""}>${label}</option>`).join("")}</select><select id="run-status" aria-label="Filter status">${["all","completed","pending","running","failed"].map(v=>`<option value="${v}" ${runStatus===v?"selected":""}>${v==="all"?"Any status":v}</option>`).join("")}</select><input id="run-date" type="date" aria-label="Created on or after" value="${h(runDate)}"></div><p class="hint">${runKind==="finished"?"Only complete MP4s with all required voice segments are shown. ":""}${visible.length} runs${visible.length>120?" · showing the latest 120":""}</p><div class="library-grid">${visible.slice(0,120).map(runCard).join("")}</div>${visible.length?"":'<div class="empty"><h2>No matching runs</h2><p>Generate a story draft to start your library.</p></div>'}`;
}
function runCard(run) {
  const file = ["reel.mp4","broll_video.mp4","broll_01.mp4","broll_02.mp4","cartridge.png","broll_still.png","broll_01_still.png","broll_02_still.png"].find(f=>run.files.includes(f));
  const image=["preview.png","cartridge.png","broll_still.png","broll_01_still.png","broll_02_still.png"].find(f=>run.files.includes(f));
  return `<article class="run-card"><div class="run-visual ${h(run.kind)}">${image?`<img loading="lazy" src="/media/${h(run.run_id)}/${image}" alt="${h(run.titles.join(", ")||run.run_id)}">`:`<span class="type-mark">${({narration:"REEL",render:"REEL",story:"01—04",cartridge:"SAVE",broll:"PLAY"})[run.kind]}</span>`}</div><div class="run-content"><span class="pill ${h(run.status)}">${run.finished?"Finished reel":h(run.kind)} · ${h(run.status)}</span><h3>${h(run.name||run.titles.join(" / ")||run.run_id)}</h3>${run.name?`<p>${h(run.titles.join(" / "))}</p>`:""}<p>${h(run.run_id)}</p><div class="run-actions">${file?`<button class="button" data-watch="${h(run.run_id)}/${file}">Open ${file.endsWith("mp4")?"video":"image"}</button><a class="button quiet" href="/media/${h(run.run_id)}/${file}" download="${h(run.run_id)}-${file}">Download</a>`:""}${run.kind==="story"&&run.status==="completed"?`<button class="button" data-import="${h(run.run_id)}">Use these stories</button>`:""}${run.files.includes("story_review.txt")?`<a class="button quiet" href="/media/${h(run.run_id)}/story_review.txt" target="_blank" rel="noopener">Read review</a>`:""}</div></div></article>`;
}
function progressBar(job) {
  const p=job.progress;
  if(!p) return `<p class="hint">This older job has no detailed progress record.</p>`;
  const done=p.completed||0, total=p.total||1;
  return `<div class="progress-copy"><span>${done} / ${total} work items complete</span><span>${Math.floor(done/total*100)}%</span></div><progress max="${total}" value="${done}" aria-label="Completed work items"></progress>`;
}
function stageProgress(job) {
  return progressBar(job)+`<p class="hint">Progress counts finished tasks, including reused assets. It does not estimate time remaining.</p><ol class="stage-progress">${(job.progress?.stages||[]).map(s=>{
    const values=Object.values(s.units), done=values.filter(v=>v==="completed").length;
    const state=values.includes("failed")?"failed":values.includes("running")?"running":values.includes("waiting")?"waiting":done===values.length?"completed":"pending";
    return `<li><div><strong>${h(s.label)}</strong><span class="pill ${state}">${state} · ${done}/${values.length}</span></div><span class="hint">${Object.entries(s.units).map(([unit,status])=>`${unit==="reel"?"Reel":unit==="intro"?"Intro":unit.replace("save_","Save ")}: ${status}`).join(" · ")}</span></li>`;
  }).join("")}</ol>`;
}
function activity() {
  return heading("Activity", "Follow each stage through to the finished video. Jobs keep running when you close the browser.") +
    `<div class="panel">${jobs.length?jobs.map(j=>`<article class="job-row"><div class="job-summary"><span class="pill ${h(j.status)}">${h(j.status)}</span><h3>${h(j.name)} · ${j.request.action==="full"?"Finished reel":h(j.request.action)}</h3><p>${h(j.phase)} · ${new Date(j.created_at).toLocaleString()}</p>${progressBar(j)}${j.error?`<p class="job-error">${h(j.error)}</p>`:""}</div><div class="run-actions"><button class="button quiet" data-job="${h(j.job_id)}">View progress</button>${["failed","interrupted"].includes(j.status)?`<button class="button" data-resume="${h(j.job_id)}" ${active()?"disabled":""}>Resume saved job</button>`:""}${j.outputs.reel&&j.status==="completed"?`<button class="button primary" data-watch="${h(j.outputs.reel)}/reel.mp4">Watch reel</button><a class="button quiet" href="/media/${h(j.outputs.reel)}/reel.mp4" download="${h(j.outputs.reel)}.mp4">Download MP4</a>`:""}</div></article>`).join(""):'<div class="empty"><h2>No jobs yet</h2><p>Generation jobs and their progress will appear here.</p></div>'}</div><section id="job-detail"></section>`;
}
async function updateLog() {
  if (!selectedJob || tab!=="jobs") return;
  const j=await api(`/api/jobs/${encodeURIComponent(selectedJob)}`), detail=$("job-detail");
  if (!detail) return;
  const open=$("technical-log")?.open||false;
  detail.innerHTML=`<div class="panel"><h2>${h(j.name)}</h2><p class="progress-line">${h(j.phase)}</p>${stageProgress(j)}${j.error?`<p class="job-error">${h(j.error)}</p><p class="hint">Completed saves are retained. Resume continues from saved checkpoints; ambiguous paid requests are not sent again automatically.</p>`:""}<details id="technical-log" ${open?"open":""}><summary>Technical log</summary><pre class="log" id="job-log">${h(j.log||"Waiting for the worker…")}</pre></details></div>`;
}
function render() {
  if (!draft) return;
  document.querySelectorAll("nav [data-tab]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.tab===tab)));
  $("view").innerHTML=({workspace,prompts:promptEditor,settings,library,jobs:activity})[tab](); header();
  if(tab==="jobs") updateLog().catch(e=>notice(e.message,true));
}
async function switchTab(next) {
  tab=next; notice("");
  if(tab==="library") runs=await api("/api/runs");
  render();
}
async function runAction(action) {
  if(action==="full"&&(storyMode==="new"||!draft.games.length)&&draft.provider!=="openai") { draft.provider="openai"; dirty=true; }
  if(dirty) await save(true);
  const request={draft_id:draft.draft_id,revision:draft.revision,action};
  if(["cartridges","stills","videos"].includes(action)) { request.save_number=mediaSave?Number(mediaSave):null; request.regenerate=forceMedia; }
  if(["stills","videos"].includes(action)) request.beat_number=mediaBeat?Number(mediaBeat):null;
  if(action==="full") request.new_stories=storyMode==="new";
  const j=await api("/api/jobs",request); selectedJob=j.job_id; lastActive=j.job_id;
  jobs=await api("/api/jobs"); tab="jobs"; notice(descriptions[action]); render();
}
async function showMedia(path) {
  let response=await fetch(`/media/${path}`,{method:"HEAD"});
  if(!response.ok&&/broll_0[12](?:_still\.png|\.mp4)$/.test(path)) {
    path=path.replace(/broll_0[12]_still\.png$/, "broll_still.png").replace(/broll_0[12]\.mp4$/, "broll_video.mp4");
    response=await fetch(`/media/${path}`,{method:"HEAD"});
  }
  if(!response.ok)throw new Error("This asset is not ready yet. Generate it first, or check its job in Activity.");
  $("modal-title").textContent=path.split("/")[0];
  $("modal-body").innerHTML=path.endsWith(".mp4")?`<video src="/media/${h(path)}" controls playsinline preload="metadata"></video>`:`<img src="/media/${h(path)}" alt="Generated asset">`;
  $("modal").showModal();
}
async function applyAdvanced() {
  const value=JSON.parse(pendingAdvanced??$("advanced-json").value);
  const proposed={...draft};
  for(const key of ["story","render","speech"]) {
    if(!value[key]||typeof value[key]!=="object"||Array.isArray(value[key]))throw new Error(`Expected an object for ${key} settings`);
    proposed[key]=value[key];
  }
  await api("/api/preview",proposed);
  draft=proposed;
  pendingAdvanced=null; dirty=true; header();
}
document.addEventListener("input",e=>{
  const el=e.target;
  if(el.dataset.path) {
    let value=el.type==="checkbox"?el.checked:el.value;
    if(el.type==="number") value=el.value===""?null:Number(el.value);
    if(["render.fps","parallel_saves","speech_parallel_saves","broll.clips_per_save"].includes(el.dataset.path)) value=Number(value);
    if(["story.candidate_reasoning_effort","story.world_review_reasoning_effort","story.world_review_prompt_version"].includes(el.dataset.path)&&value==="") value=null;
    set(el.dataset.path,value);
    const match=el.dataset.path.match(/^games\.(\d+)\.narration$/);
    if(match) $(`words-${match[1]}`).textContent=`${el.value.trim().split(/\s+/).filter(Boolean).length} words`;
  }
  if(el.id==="prompt-body") { draft.prompts[selectedPrompt]=el.value; dirty=true; header(); }
  if(el.id==="advanced-json") { pendingAdvanced=el.value; dirty=true; header(); }
  if(el.id==="run-search") { const position=el.selectionStart; runSearch=el.value; $("view").innerHTML=library(); $("run-search").focus(); $("run-search").setSelectionRange(position,position); }
});
document.addEventListener("change",async e=>{
  try {
    if(e.target.id==="draft-select") { if(canLeave()) await loadDraft(e.target.value); else draftMenu(); }
    if(e.target.id==="prompt-select") { selectedPrompt=e.target.value; render(); }
    if(e.target.id==="story-mode") { storyMode=e.target.value; render(); }
    if(e.target.id==="run-status") { runStatus=e.target.value; render(); }
    if(e.target.id==="run-date") { runDate=e.target.value; render(); }
    if(e.target.dataset.path==="broll.video_model") {
      if(draft.broll.video_model==="MiniMax-H3-Max") { if(draft.broll.resolution==="2K")set("broll.resolution","768P"); if(draft.broll.duration<5)set("broll.duration",5); }
      else if(draft.broll.resolution==="480P")set("broll.resolution","768P");
      render();
    }
    if(e.target.id==="run-kind") { runKind=e.target.value; render(); }
    if(e.target.id==="media-save") mediaSave=e.target.value;
    if(e.target.id==="media-beat") mediaBeat=e.target.value;
    if(e.target.id==="force-media") forceMedia=e.target.checked;
    if(e.target.id==="all-prompts") { allPrompts=e.target.checked; render(); }
    if(e.target.dataset.path==="provider") render();
  } catch(error) { notice(error.message,true); }
});
document.addEventListener("click",async e=>{
  const b=e.target.closest("button"); if(!b||b.disabled) return;
  try {
    if(b.dataset.tab) await switchTab(b.dataset.tab);
    else if(b.dataset.action) { b.disabled=true; await runAction(b.dataset.action); }
    else if(b.dataset.watch) await showMedia(b.dataset.watch);
    else if(b.dataset.job) { selectedJob=b.dataset.job; await updateLog(); }
    else if(b.dataset.resume) { const j=await api("/api/resume",{job_id:b.dataset.resume}); selectedJob=lastActive=j.job_id; jobs=await api("/api/jobs"); render(); }
    else if(b.dataset.import) { if(!canLeave())return; draft=await api("/api/import",{run_id:b.dataset.import}); dirty=false; pendingAdvanced=null; storyMode="existing"; revisions=await api(`/api/revisions/${draft.draft_id}`); drafts=await api("/api/drafts"); draftMenu(); tab="workspace"; render(); notice("Stories imported into a new draft. Matching saved media is attached where available."); }
    else if(b.id==="new-draft") await newDraft();
    else if(b.id==="duplicate") await newDraft(true);
    else if(b.id==="save") { await save(); if(["prompts","settings"].includes(tab))render(); }
    else if(b.id==="save-preset") {
      const name=$("preset-name").value;
      if(dirty)await save(true);
      await api("/api/presets",{draft,name}); presets=await api("/api/presets"); render(); notice("Preset revision saved. Apply it to any draft from Models & settings.");
    }
    else if(b.id==="apply-preset"||b.id==="restore-revision") {
      const preset_id=$("preset-select").value, revision=Number($("revision-select").value);
      if(dirty)await save(true);
      draft=await api(b.id==="apply-preset"?"/api/apply-preset":"/api/restore-revision",{draft,preset_id,revision,prompt:tab==="prompts"?selectedPrompt:null});
      revisions=await api(`/api/revisions/${draft.draft_id}`); dirty=false; pendingAdvanced=null; render(); notice("Saved to this reel. Existing generated assets are retained; new jobs check whether they still match.");
    }
    else if(b.id==="close-modal") $("modal").close();
    else if(b.id==="refresh-library") { runs=await api("/api/runs"); render(); }
    else if(b.id==="reset-prompt") { defaults=defaults||await api("/api/default-prompts"); draft.prompts[selectedPrompt]=defaults[selectedPrompt]; dirty=true; render(); }
    else if(b.id==="apply-json") { await applyAdvanced(); render(); notice("Advanced settings applied. Save changes to keep them."); }
    else if(b.id==="preview-prompts") { const previews=await api("/api/preview",draft); $("modal-title").textContent="Compiled prompts · no API calls"; $("modal-body").innerHTML=previews.map((p,i)=>`<h2>Save 0${i+1} · ${h(draft.games[i].title)}</h2>${Object.entries(p).map(([key,text])=>`<details><summary>${h(key)}</summary><pre>${h(text)}</pre></details>`).join("")}`).join(""); $("modal").showModal(); }
  } catch(error) { notice(error.message,true); if(b.dataset.action)b.disabled=false; }
});
$("modal").addEventListener("close",()=>{$("modal-body").innerHTML="";});
window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});
async function poll() {
  if(polling)return; polling=true;
  try {
    const previous=active()?.job_id || lastActive;
    const oldStatus=JSON.stringify(jobs); jobs=await api("/api/jobs");
    if(previous&&!active()) {
      lastActive=null;
      const ended=jobs.find(j=>j.job_id===previous);
      if(ended?.draft_id===draft?.draft_id&&!dirty) { draft=await api(`/api/drafts/${encodeURIComponent(draft.draft_id)}`); storyMode=draft.games.length?"existing":"new"; drafts=await api("/api/drafts"); draftMenu(); render(); }
      if(ended) notice(ended.status==="completed"?"Generation finished. Your draft and saved runs are ready.":ended.error||"Generation stopped. Check Activity.",ended.status!=="completed");
    }
    lastActive=active()?.job_id||null; header();
    if(tab==="jobs") {
      if(oldStatus!==JSON.stringify(jobs)) { const top=window.scrollY; render(); window.scrollTo(0,top); }
      else await updateLog();
    }
  } catch(error) { notice(`Studio connection: ${error.message}`,true); }
  finally { polling=false; }
}
async function init() {
  const boot=await api("/api/bootstrap"); token=boot.token; drafts=boot.drafts; jobs=boot.jobs; credentials=boot.credentials;
  lastActive=active()?.job_id||null; presets=await api("/api/presets");
  if(drafts.length) await loadDraft(drafts[0].draft_id); else await newDraft();
  setInterval(poll,3000);
}
init().catch(error=>notice(error.message,true));
