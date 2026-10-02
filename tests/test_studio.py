"""Local Studio boundaries, snapshotting and orchestration; no paid providers."""

import json
import shutil
import threading
import time
from http.client import HTTPConnection

import pytest
from pydantic import ValidationError
from test_narration import FakeSpeech, narration_assets  # noqa: F401
from test_rendering import local_assets  # noqa: F401

from save_reel.broll import BrollPipeline
from save_reel.cartridge import CartridgePipeline
from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.storage import RunStore
from save_reel.story_pipeline import StoryWorkflow
from save_reel.studio_jobs import StudioJobs
from save_reel.studio_models import JobRequest, StudioDraft, StudioGame
from save_reel.studio_server import StudioServer
from save_reel.studio_store import StudioStore, games_from_story, snapshot_prompts, write_json
from save_reel.studio_worker import ProductionJob


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    return StoryWorkflow(MockStoryProvider()).create(
        ConceptRequest(),
        runs_dir=tmp_path_factory.mktemp("studio-stories"),
        run_id="fixture-story",
        random_seed=41,
    )


@pytest.fixture
def store(tmp_path):
    return StudioStore(tmp_path)


def prepare_job(
    store, draft, action="stories", *, job_id="studio-test", regenerate=False, save_number=None
):
    folder = store.jobs / job_id
    folder.mkdir()
    request = JobRequest(
        draft_id=draft.draft_id,
        revision=draft.revision,
        action=action,
        regenerate=regenerate,
        save_number=save_number,
    )
    write_json(folder / "draft.json", draft.model_dump(mode="json"))
    snapshot_prompts(draft, folder / "prompts")
    write_json(
        folder / "job.json",
        {
            "job_id": job_id,
            "draft_id": draft.draft_id,
            "name": draft.name,
            "request": request.model_dump(mode="json"),
            "status": "queued",
            "created_at": "2026-10-02T00:00:00Z",
            "outputs": {},
            "pid": None,
        },
    )
    return ProductionJob(store, job_id)


def test_draft_roundtrip_validation_and_optimistic_revision(store):
    draft = store.create("My reel")
    assert store.load(draft.draft_id) == draft
    stale = draft.model_copy(deep=True)
    draft.story.narration_model = "configured-writer"
    saved = store.save(draft)
    assert saved.revision == draft.revision + 1
    assert saved.story.narration_model == "configured-writer"
    with pytest.raises(ValueError, match="another tab"):
        store.save(stale)
    malformed = saved.model_dump()
    malformed["broll"]["duration"] = 99
    with pytest.raises(ValidationError):
        StudioDraft.model_validate(malformed)


def test_template_validation_and_snapshot_isolation(store):
    draft = store.create()
    key = "story_narration_prose_candidate/v3.txt"
    draft.prompts[key] += "\nUse short words.\n"
    draft = store.save(draft)
    job = prepare_job(store, draft)
    draft.prompts[key] += "A later edit."
    store.save(draft)
    assert "A later edit" not in (job.prompts / key).read_text()
    assert "Use short words" in (job.prompts / key).read_text()
    current = store.load(draft.draft_id)
    current.prompts["cartridge/v2.txt"] += " [UNKNOWN_VARIABLE]"
    with pytest.raises(ValueError, match="unknown cartridge"):
        store.save(current)
    current.prompts["../../.env"] = "bad"
    with pytest.raises(ValueError, match="catalog"):
        store.save(current)


def test_mock_story_job_exports_four_games_and_never_calls_media(store, monkeypatch):
    draft = store.create()
    draft.seed = 41
    draft = store.save(draft)
    job = prepare_job(store, draft)
    monkeypatch.setattr(ProductionJob, "media", lambda *a: pytest.fail("Unexpected media stage"))
    monkeypatch.setattr(ProductionJob, "narrate", lambda *a: pytest.fail("Unexpected TTS stage"))
    job.execute()
    assert job.job["status"] == "completed", job.job.get("error")
    saved = store.load(draft.draft_id)
    assert len(saved.games) == 4
    assert saved.source_story == "studio-test-story"
    assert all(g.narration and g.cartridge.hint_scene_description for g in saved.games)
    # Existing story stage cache must be reusable by the same worker.
    before = (store.runs / saved.source_story / "story.json").read_bytes()
    resumed = ProductionJob(store, "studio-test")
    resumed.stories()
    assert (store.runs / saved.source_story / "story.json").read_bytes() == before
    assert not (store.runs / ".story-history").exists()


def test_selective_media_reuses_other_saves_and_changed_prompt_forks(store, story, monkeypatch):
    draft = store.create()
    draft.games = games_from_story(story)
    draft = store.save(draft)
    first = prepare_job(store, draft, "cartridges", job_id="first")
    calls = []
    monkeypatch.setattr(ProductionJob, "command", lambda self, *args: calls.append(args))
    first.execute()
    assert first.job["status"] == "completed"
    original = [g.cartridge_run for g in first.draft.games]
    calls.clear()
    second = prepare_job(
        store,
        store.load(draft.draft_id),
        "cartridges",
        job_id="second",
        regenerate=True,
        save_number=2,
    )
    second.execute()
    assert len(calls) == 1
    assert second.draft.games[1].cartridge_run == "second-cart02"
    assert [second.draft.games[i].cartridge_run for i in (0, 2, 3)] == [
        original[i] for i in (0, 2, 3)
    ]
    # Resume a forced job uses the same branch, not another paid image.
    second.media("cartridges")
    assert second.draft.games[1].cartridge_run == "second-cart02"
    changed = store.load(draft.draft_id)
    changed.prompts["cartridge/v2.txt"] += "\nUse a small red notch.\n"
    changed = store.save(changed)
    third = prepare_job(store, changed, "cartridges", job_id="third", save_number=1)
    third.execute()
    assert third.draft.games[0].cartridge_run == "third-cart01"
    prepared = CartridgePipeline.load(RunStore(store.runs / "third-cart01"))
    assert "small red notch" in prepared.prompt.text


def test_stills_skip_minimax_and_video_resume_keeps_same_run(store, story, monkeypatch):
    draft = store.create()
    draft.games = games_from_story(story)
    draft = store.save(draft)
    calls = []
    monkeypatch.setattr(ProductionJob, "command", lambda self, *args: calls.append(args))
    stills = prepare_job(store, draft, "stills", save_number=3)
    stills.execute()
    assert stills.job["status"] == "completed"
    assert len(calls) == 1 and "--image-only" in calls[0]
    reference = stills.draft.games[2].broll_run
    video = prepare_job(store, stills.draft, "videos", job_id="video", save_number=3)
    video.execute()
    assert video.job["status"] == "failed"  # Fake command has not finished the remote task.
    assert "Resume this job" in video.job["error"]
    assert video.draft.games[2].broll_run == reference
    assert "--image-only" not in calls[-1]
    assert BrollPipeline.load(RunStore(store.runs / reference)).settings.duration == 10


def test_full_job_orders_existing_stages(store, story, monkeypatch):
    draft = store.create()
    draft.provider = "openai"
    draft = store.save(draft)
    order = []

    def fake_stories(self):
        order.append("stories")
        self.draft.games = games_from_story(story)

    monkeypatch.setattr(ProductionJob, "stories", fake_stories)
    monkeypatch.setattr(ProductionJob, "media", lambda self, kind: order.append(kind))
    monkeypatch.setattr(ProductionJob, "render", lambda self: order.append("render"))
    def narrate(self):
        order.append("narrate")
        self.job["outputs"]["reel"] = "fake-final"

    monkeypatch.setattr(ProductionJob, "narrate", narrate)
    monkeypatch.setattr("save_reel.studio_store.is_finished_reel", lambda _: True)
    job = prepare_job(store, draft, "full")
    job.execute()
    assert job.job["status"] == "completed"
    assert order == ["stories", "cartridges", "stills", "videos", "render", "narrate"]
    order.clear()
    job.execute()
    assert order == ["cartridges", "stills", "videos", "render", "narrate"]


@pytest.fixture
def server(tmp_path):
    server = StudioServer(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def request(server, path, body=None, headers=None, method=None):
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    data = None if body is None else json.dumps(body)
    auth = {"Content-Type": "application/json", "X-Studio-Token": server.token}
    conn.request(
        method or ("GET" if body is None else "POST"),
        path,
        body=data,
        headers={**auth, **(headers or {})},
    )
    result = conn.getresponse()
    status, response_headers, content = result.status, dict(result.getheaders()), result.read()
    conn.close()
    return status, response_headers, content


def test_http_edit_preview_import_and_no_credentials_disclosure(server, story, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-value")
    status, _, body = request(server, "/api/bootstrap")
    assert status == 200 and b"test-private-value" not in body
    assert json.loads(body)["credentials"]["OPENAI_API_KEY"] is True
    status, _, body = request(server, "/api/drafts", {"name": "Browser draft"})
    assert status == 200
    draft = json.loads(body)
    draft["name"] = "Edited draft"
    assert request(server, "/api/save", draft)[0] == 200
    assert request(server, "/api/save", draft)[0] == 400  # stale revision
    draft["games"] = [g.model_dump(mode="json") for g in games_from_story(story)]
    status, _, body = request(server, "/api/preview", draft)
    assert status == 200
    assert len(json.loads(body)) == 4
    assert "[GAME_TITLE]" not in json.loads(body)[0]["still"]
    assert not list(server.store.runs.glob("*/broll.json"))
    run_dir = server.store.runs / story.run_id
    run_dir.mkdir()
    (run_dir / "story.json").write_text(story.model_dump_json())
    status, _, body = request(server, "/api/import", {"run_id": story.run_id})
    assert status == 200 and len(json.loads(body)["games"]) == 4


def test_http_rejects_cross_origin_host_and_path_escapes(server, tmp_path):
    assert request(server, "/api/drafts", {}, {"Origin": "https://untrusted.example"})[0] == 403
    assert request(server, "/api/drafts", {}, {"X-Studio-Token": "wrong"})[0] == 403
    assert request(server, "/api/bootstrap", headers={"Host": "untrusted.example"})[0] == 403
    assert request(server, "/media/../.env")[0] == 404
    assert request(server, "/api/drafts/../../.env")[0] == 400
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reel.mp4").write_bytes(b"private")
    (server.store.runs / "escape").symlink_to(outside, target_is_directory=True)
    assert request(server, "/media/escape/reel.mp4")[0] == 400


def test_video_ranges_and_static_assets(server):
    folder = server.store.runs / "test-video"
    folder.mkdir()
    (folder / "reel.mp4").write_bytes(b"0123456789")
    status, headers, body = request(
        server, "/media/test-video/reel.mp4", headers={"Range": "bytes=2-5"}
    )
    assert status == 206 and body == b"2345"
    assert headers["Content-Range"] == "bytes 2-5/10"
    assert request(server, "/media/test-video/reel.mp4", headers={"Range": "bytes=-3"})[2] == b"789"
    assert request(server, "/media/test-video/reel.mp4", headers={"Range": "bytes=99-"})[0] == 416
    assert request(server, "/media/test-video/reel.mp4", method="HEAD")[2] == b""
    for path in ("/", "/app.js", "/style.css"):
        status, headers, body = request(server, path)
        assert status == 200 and body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_real_background_mock_job_and_resume_guard(store):
    draft = store.create("Offline smoke")
    draft.seed = 41
    draft = store.save(draft)
    jobs = StudioJobs(store)
    job = jobs.start(JobRequest(draft_id=draft.draft_id, revision=draft.revision, action="stories"))
    with pytest.raises(ValueError, match="running"):
        jobs.start(JobRequest(draft_id=draft.draft_id, revision=draft.revision, action="stories"))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = jobs.read(job["job_id"])
        if current["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert current["status"] == "completed", current
    assert len(store.load(draft.draft_id).games) == 4
    with pytest.raises(ValueError, match="already complete"):
        jobs.resume(job["job_id"])
    for child in jobs.children:
        child.wait(timeout=5)


def test_edited_draft_cannot_resume_old_snapshot(store):
    draft = store.create()
    job = prepare_job(store, draft)
    jobs = StudioJobs(store)
    draft.name = "New revision"
    store.save(draft)
    with pytest.raises(ValueError, match="edited"):
        jobs.resume(job.job_id)


def test_studio_renders_narrates_and_reuses_unchanged_speech(
    store,
    local_assets,  # noqa: F811 -- shared synthetic fixtures
    narration_assets,  # noqa: F811
    monkeypatch,
):
    from save_reel.providers import elevenlabs_speech
    from save_reel.render_models import RenderSettings
    from save_reel.rendering import probe_media

    _, sources, collection, _ = local_assets
    _, audio, script = narration_assets
    provider = FakeSpeech(audio)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(elevenlabs_speech, "ElevenLabsSpeechProvider", lambda *args: provider)
    games = []
    for original, spoken in zip(collection.games, script.games, strict=True):
        for run_id in (original.cartridge_run_id, original.broll_run_ids[0]):
            shutil.copytree(sources / run_id, store.runs / run_id)
        cart = CartridgePipeline.load(RunStore(store.runs / original.cartridge_run_id))
        roll = BrollPipeline.load(RunStore(store.runs / original.broll_run_ids[0]))
        games.append(
            StudioGame(
                title=original.title,
                narration=spoken.text,
                cartridge=cart.values,
                environment=roll.values,
                motion=roll.motion,
                cartridge_run=cart.run_id,
                broll_run=roll.run_id,
            )
        )
    draft = store.create()
    draft.games = tuple(games)
    draft.render = RenderSettings(
        width=216, height=384, intro_seconds=1, countdown_seconds=1, clip_seconds=0.5
    )
    draft = store.save(draft)
    first = prepare_job(store, draft, "narrate", job_id="first")
    first.execute()
    assert first.job["status"] == "completed", first.job.get("error")
    assert provider.calls == 5
    result = store.runs / first.job["outputs"]["reel"] / "reel.mp4"
    assert probe_media(result)["streams"][0]["width"] == 216
    revised = store.load(draft.draft_id)
    revised.games[1].narration = "Walk slowly through this world."
    revised = store.save(revised)
    second = prepare_job(store, revised, "narrate", job_id="second")
    second.execute()
    assert second.job["status"] == "completed", second.job.get("error")
    assert provider.calls == 6  # Only the edited save needs a new recording.
