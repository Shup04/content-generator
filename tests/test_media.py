# Optional media dependencies must be checked before importing their adapters.
# ruff: noqa: E402
import base64
import json
from io import BytesIO
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
openai = pytest.importorskip("openai")
Image = pytest.importorskip("PIL.Image")
pytest.importorskip("dotenv")

from save_reel.broll import BrollPipeline
from save_reel.cli import main
from save_reel.media_models import BrollSettings, BrollValues, ImageSettings, MotionValues
from save_reel.models import StageStatus
from save_reel.providers.media import GeneratedImage, MediaError, VideoTask
from save_reel.providers.minimax_video import MiniMaxVideoProvider
from save_reel.providers.openai_image import OpenAIImageProvider
from save_reel.storage import RunStore


@pytest.fixture
def png():
    stream = BytesIO()
    Image.new("RGB", (864, 1536), "turquoise").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def media_input():
    examples = Path(__file__).resolve().parents[1] / "examples"
    return (
        BrollValues.model_validate_json((examples / "deep-end.json").read_text()),
        MotionValues.model_validate_json((examples / "deep-end-motion.json").read_text()),
    )


@pytest.mark.parametrize(
    "settings", [BrollSettings(), ImageSettings(), ImageSettings(image_background="transparent")]
)
def test_openai_request_and_png_validation(settings):
    requests = []
    stream = BytesIO()
    size = tuple(int(edge) for edge in settings.image_size.split("x"))
    if settings.image_background == "transparent":
        source = Image.new("RGBA", size, (0, 0, 0, 0))
        source.paste((0, 200, 200, 255), (100, 100, size[0] - 100, size[1] - 100))
    else:
        source = Image.new("RGB", size, "turquoise")
    source.save(stream, format="PNG")
    png = stream.getvalue()

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "req_test"},
            json={"created": 1, "data": [{"b64_json": base64.b64encode(png).decode()}]},
        )

    with openai.OpenAI(
        api_key="test-openai-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        image = OpenAIImageProvider(client).generate("An old game screenshot", settings)
    assert image.content == png
    assert image.request_id == "req_test"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/images/generations"
    assert json.loads(requests[0].content) == {
        "model": "gpt-image-2.5-sunburst",
        "prompt": "An old game screenshot",
        "size": settings.image_size,
        "quality": "medium",
        "output_format": "png",
        "background": settings.image_background,
        "n": 1,
    }


@pytest.mark.parametrize("mode,alpha", [("RGB", 255), ("RGBA", 255), ("RGBA", 0)])
def test_openai_rejects_fake_transparency_and_empty_cutouts(mode, alpha):
    stream = BytesIO()
    Image.new(mode, (1536, 1024), (100, 150, 200, alpha)[:len(mode)]).save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": encoded}]}
    ))
    with openai.OpenAI(
        api_key="test-key", http_client=httpx.Client(transport=transport)
    ) as client:
        with pytest.raises(MediaError, match="transparent background"):
            OpenAIImageProvider(client).generate(
                "A transparent cutout", ImageSettings(image_background="transparent")
            )


def test_openai_rejects_dimensions_that_do_not_match_requested_size(png):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": base64.b64encode(png).decode()}]}
    ))
    with openai.OpenAI(
        api_key="test-key", http_client=httpx.Client(transport=transport)
    ) as client:
        with pytest.raises(MediaError, match="dimensions"):
            OpenAIImageProvider(client).generate("A cartridge", ImageSettings())


def test_openai_does_not_retry_or_expose_raw_error():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "secret-canary"}})

    with openai.OpenAI(
        api_key="test-openai-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(MediaError, match="HTTP 500") as error:
            OpenAIImageProvider(client).generate("A pool", BrollSettings())
    assert "secret-canary" not in str(error.value)
    assert len(requests) == 1


def test_minimax_v2_payload_poll_and_download_without_credentials(png):
    requests = []
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"test video"

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/video_generation":
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/v2/query/video_generation/task-1":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "status": "succeeded",
                        "content": {"url": "https://cdn.example/video.mp4"},
                        "usage": {"output_seconds": 5},
                    }
                },
            )
        return httpx.Response(200, content=mp4)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = MiniMaxVideoProvider("test-minimax-key", client)
        task_id = provider.submit(png, "Small pool ripples", BrollSettings())
        task = provider.query(task_id)
        assert provider.download(task.download_url) == mp4
    body = json.loads(requests[0].content)
    assert body["model"] == "MiniMax-H3"
    assert body["resolution"] == "768P"
    assert body["duration"] == 5
    assert body["ratio"] == "adaptive"
    assert body["content"][1]["role"] == "first_frame"
    assert base64.b64decode(body["content"][1]["image_url"]["url"].split(",", 1)[1]) == png
    assert requests[0].headers["Authorization"] == "Bearer test-minimax-key"
    assert requests[1].headers["Authorization"] == "Bearer test-minimax-key"
    assert "Authorization" not in requests[2].headers
    assert task.usage == {"output_seconds": 5}


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_minimax_task_states(status):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"task": {"status": status}})
        )
    ) as client:
        assert MiniMaxVideoProvider("test-key", client).query("123").status == status


def test_minimax_rejects_missing_download_url_and_non_mp4():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"task": {"status": "succeeded"}})
        )
    ) as client:
        provider = MiniMaxVideoProvider("test-key", client)
        with pytest.raises(MediaError, match="download URL"):
            provider.query("123")
        with pytest.raises(MediaError, match="not an MP4"):
            provider.download("https://cdn.example/video.mp4")


class FakeImageProvider:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def generate(self, prompt, settings):
        self.calls += 1
        return GeneratedImage(self.content, request_id="req_test")


class FakeVideoProvider:
    def __init__(self):
        self.submissions = 0
        self.queries = 0
        self.download_failure = False
        self.submit_failure = False
        self.status = "succeeded"

    def submit(self, image, prompt, settings):
        self.submissions += 1
        assert image
        assert "waterfalls flow gently" in prompt
        if self.submit_failure:
            raise MediaError("Submission timed out")
        return "task-123"

    def query(self, task_id):
        self.queries += 1
        assert task_id == "task-123"
        return VideoTask(self.status, "https://cdn.example/video.mp4")

    def download(self, url):
        if self.download_failure:
            raise MediaError("Download failed")
        return b"\x00\x00\x00\x18ftypisom" + b"test video"


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_broll_end_to_end_and_completed_resume_are_idempotent(tmp_path, media_input, png, version):
    image = FakeImageProvider(png)
    video = FakeVideoProvider()
    pipeline = BrollPipeline(image, video)
    store = pipeline.prepare(
        *media_input,
        BrollSettings(),
        runs_dir=tmp_path,
        run_id="deep-end",
        still_template_version=version,
        video_template_version=version,
    )
    run = pipeline.execute(store)
    assert run.status == StageStatus.COMPLETED
    assert run.video_task_id == "task-123"
    assert pipeline.load(store) == run
    assert (store.run_dir / "broll_still.png").read_bytes() == png
    assert (store.run_dir / "broll_video.mp4").is_file()
    assert not (store.run_dir / "manifest.json").exists()
    assert not (store.run_dir / ".broll.lock").exists()
    pipeline.execute(store)
    assert (image.calls, video.submissions, video.queries) == (1, 1, 1)
    resumed = pipeline.load(store)
    assert resumed.still_prompt == run.still_prompt
    assert resumed.video_prompt == run.video_prompt


@pytest.mark.parametrize(
    "flags,versions",
    [
        ([], ("v2", "v2")),
        (["--still-template-version", "v1"], ("v1", "v2")),
        (["--video-template-version", "v1"], ("v2", "v1")),
    ],
)
def test_generate_cli_selects_and_saves_prompt_versions(tmp_path, monkeypatch, flags, versions):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    # Compile and checkpoint through the real CLI without making generation requests.
    monkeypatch.setattr(BrollPipeline, "execute", lambda *args, **kwargs: None)
    examples = Path(__file__).resolve().parents[1] / "examples"
    assert (
        main(
            [
                "generate-broll",
                "--values",
                str(examples / "deep-end.json"),
                "--motion-values",
                str(examples / "deep-end-motion.json"),
                "--runs-dir",
                str(tmp_path),
                "--run-id",
                "versions",
                "--env-file",
                str(tmp_path / "absent.env"),
                *flags,
            ]
        )
        == 0
    )
    store = RunStore(tmp_path / "versions")
    run = BrollPipeline.load(store)
    assert (run.still_prompt.template.version, run.video_prompt.template.version) == versions
    assert store.run_dir.joinpath("broll_still_prompt.txt").read_text() == run.still_prompt.text
    assert store.run_dir.joinpath("broll_video_prompt.txt").read_text() == run.video_prompt.text
    assert not run.image_attempted and not run.video_attempted


def test_download_failure_resume_uses_existing_image_and_task(tmp_path, media_input, png):
    image = FakeImageProvider(png)
    video = FakeVideoProvider()
    video.download_failure = True
    pipeline = BrollPipeline(image, video)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    with pytest.raises(MediaError, match="Download failed"):
        pipeline.execute(store)
    failed = pipeline.load(store)
    assert failed.video_task_id == "task-123"
    assert failed.stages["image_generation"].status == StageStatus.COMPLETED
    video.download_failure = False
    assert pipeline.execute(store).status == StageStatus.COMPLETED
    assert (image.calls, video.submissions) == (1, 1)


def test_ambiguous_submit_is_not_automatically_repeated(tmp_path, media_input, png):
    image = FakeImageProvider(png)
    video = FakeVideoProvider()
    video.submit_failure = True
    pipeline = BrollPipeline(image, video)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    with pytest.raises(MediaError, match="Submission timed out"):
        pipeline.execute(store)
    with pytest.raises(MediaError, match="no duplicate"):
        pipeline.execute(store)
    assert (image.calls, video.submissions) == (1, 1)


def test_image_only_can_resume_without_openai(tmp_path, media_input, png):
    image = FakeImageProvider(png)
    pipeline = BrollPipeline(image, None)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    pending = pipeline.execute(store, image_only=True)
    assert pending.status == StageStatus.PENDING
    assert pending.stages["image_generation"].status == StageStatus.COMPLETED
    assert BrollPipeline(None, FakeVideoProvider()).execute(store).status == StageStatus.COMPLETED
    assert image.calls == 1


def test_corrupted_saved_image_is_not_sent_to_minimax(tmp_path, media_input, png):
    video = FakeVideoProvider()
    pipeline = BrollPipeline(FakeImageProvider(png), video)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    pipeline.execute(store, image_only=True)
    (store.run_dir / "broll_still.png").write_bytes(b"corrupt")
    with pytest.raises(MediaError, match="checksum"):
        pipeline.execute(store)
    assert video.submissions == 0


def test_missing_keys_fail_before_creating_run(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    runs = tmp_path / "runs"
    assert (
        main(
            [
                "generate-broll",
                "--values",
                "unused.json",
                "--motion-values",
                "unused.json",
                "--runs-dir",
                str(runs),
                "--env-file",
                str(tmp_path / "absent.env"),
            ]
        )
        == 1
    )
    assert "Missing OPENAI_API_KEY, MINIMAX_API_KEY" in caplog.text
    assert not runs.exists()


def test_poll_timeout_can_resume_same_remote_task(tmp_path, media_input, png, monkeypatch):
    image = FakeImageProvider(png)
    video = FakeVideoProvider()
    video.status = "running"
    pipeline = BrollPipeline(image, video)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    with monkeypatch.context() as patch:
        times = iter([0, 2])
        patch.setattr("save_reel.broll.time.monotonic", lambda: next(times))
        with pytest.raises(MediaError, match="wait timed out"):
            pipeline.execute(store, wait_timeout=1)
    assert pipeline.load(store).video_task_id == "task-123"
    video.status = "succeeded"
    assert pipeline.execute(store).status == StageStatus.COMPLETED
    assert (image.calls, video.submissions) == (1, 1)


def test_ambiguous_image_failure_does_not_repeat_paid_request(tmp_path, media_input):
    class FailingImage:
        calls = 0

        def generate(self, prompt, settings):
            self.calls += 1
            raise MediaError("Image connection timed out")

    image = FailingImage()
    video = FakeVideoProvider()
    pipeline = BrollPipeline(image, video)
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    with pytest.raises(MediaError, match="Image connection timed out"):
        pipeline.execute(store)
    with pytest.raises(MediaError, match="already attempted"):
        pipeline.execute(store)
    assert image.calls == 1
    assert video.submissions == 0


def test_completed_resume_cli_needs_no_keys(tmp_path, media_input, png, monkeypatch):
    pipeline = BrollPipeline(FakeImageProvider(png), FakeVideoProvider())
    store = pipeline.prepare(*media_input, BrollSettings(), runs_dir=tmp_path)
    pipeline.execute(store)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert (
        main(["resume-broll", str(store.run_dir), "--env-file", str(tmp_path / "absent.env")]) == 0
    )
