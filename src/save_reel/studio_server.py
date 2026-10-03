"""Loopback-only Studio HTTP API and static UI; no additional web dependencies."""

import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from save_reel.studio_jobs import StudioJobs
from save_reel.studio_models import JobRequest, StudioDraft
from save_reel.studio_store import (
    MEDIA_FILES,
    StudioStore,
    bundled_prompts,
    compile_game,
    identifier,
    validate_prompts,
    write_json,
)


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, project: Path, port=8765):
        self.store = StudioStore(project)
        self.jobs = StudioJobs(self.store)
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), StudioHandler)

    @property
    def origins(self):
        port = self.server_address[1]
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


class StudioHandler(BaseHTTPRequestHandler):
    server: StudioServer

    def log_message(self, format, *args):
        pass  # Poll requests must not fill the daemon log.

    def headers_for(self, status, mime, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; "
            "media-src 'self'; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def respond(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.headers_for(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def guard(self, write=False):
        host = "http://" + self.headers.get("Host", "")
        if host not in self.server.origins:
            raise PermissionError("Studio accepts localhost requests only")
        if write:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in self.server.origins:
                raise PermissionError("Cross-origin writes are not allowed")
            if not secrets.compare_digest(
                self.headers.get("X-Studio-Token", ""), self.server.token
            ):
                raise PermissionError("Reload Studio before making changes")

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Use application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 2_000_000:
            raise ValueError("Request must be between 1 byte and 2 MB")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.guard()
            path = unquote(urlsplit(self.path).path)
            store = self.server.store
            if path == "/api/bootstrap":
                keys = {}
                try:
                    from dotenv import dotenv_values

                    keys = dotenv_values(store.project / ".env")
                except ImportError:
                    pass
                return self.respond(
                    {
                        "token": self.server.token,
                        "project": str(store.project),
                        "drafts": store.list_drafts(),
                        "jobs": self.server.jobs.list(),
                        "credentials": {
                            name: bool(os.getenv(name) or keys.get(name))
                            for name in ("OPENAI_API_KEY", "MINIMAX_API_KEY", "ELEVENLABS_API_KEY")
                        },
                    }
                )
            if path == "/api/drafts":
                return self.respond(store.list_drafts())
            if path.startswith("/api/drafts/"):
                return self.respond(
                    store.load(path.removeprefix("/api/drafts/")).model_dump(mode="json")
                )
            if path == "/api/default-prompts":
                return self.respond(bundled_prompts())
            if path == "/api/presets":
                return self.respond(store.list_presets())
            if path.startswith("/api/revisions/"):
                return self.respond(store.list_revisions(path.removeprefix("/api/revisions/")))
            if path == "/api/runs":
                return self.respond(store.list_runs())
            if path == "/api/jobs":
                return self.respond(self.server.jobs.list())
            if path.startswith("/api/jobs/"):
                job_id = identifier(path.removeprefix("/api/jobs/"))
                job = self.server.jobs.read(job_id)
                log = store.jobs / job_id / "job.log"
                content = b""
                if log.exists():
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 30_000))
                        content = stream.read(30_000)
                return self.respond({**job, "log": content.decode(errors="replace")})
            if path.startswith("/media/"):
                parts = path.removeprefix("/media/").split("/")
                if len(parts) != 2 or parts[1] not in MEDIA_FILES:
                    raise FileNotFoundError("Unknown media file")
                folder = store.run_dir(parts[0])
                media = folder / parts[1]
                if media.resolve().parent != folder.resolve():
                    raise PermissionError("Media must be inside the run directory")
                return self.serve_file(media)
            if path in ("/", "/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[1:]
                resource = files("save_reel").joinpath("assets", "studio", name)
                body = resource.read_bytes()
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self.headers_for(200, mime + "; charset=utf-8", len(body))
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            raise FileNotFoundError("Unknown route")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.failure(exc)

    def do_POST(self):
        try:
            self.guard(write=True)
            data = self.body()
            path = urlsplit(self.path).path
            store, jobs = self.server.store, self.server.jobs
            with jobs.lock:
                if path == "/api/drafts":
                    source = store.load(data["copy_from"]) if data.get("copy_from") else None
                    result = store.create(data.get("name", "Untitled reel"), source).model_dump(
                        mode="json"
                    )
                elif path == "/api/save":
                    draft = StudioDraft.model_validate(data)
                    jobs.check_editable(draft.draft_id)
                    result = store.save(draft).model_dump(mode="json")
                elif path == "/api/import":
                    result = store.import_story(data["run_id"]).model_dump(mode="json")
                elif path == "/api/presets":
                    draft = StudioDraft.model_validate(data["draft"])
                    result = store.save_preset(draft, data["name"])
                elif path in ("/api/apply-preset", "/api/restore-revision"):
                    draft = StudioDraft.model_validate(data["draft"])
                    jobs.check_editable(draft.draft_id)
                    if path == "/api/apply-preset":
                        draft = store.apply_preset(draft, data["preset_id"])
                    else:
                        draft = store.restore_revision(draft, data["revision"], data.get("prompt"))
                    result = draft.model_dump(mode="json")
                elif path == "/api/preview":
                    draft = StudioDraft.model_validate(data)
                    with tempfile.TemporaryDirectory(dir=store.root) as directory:
                        root = Path(directory)
                        validate_prompts(draft, root)
                        result = [
                            {
                                key: prompt.text
                                for beat in game.clip_numbers()
                                for name, prompt in compile_game(draft, game, root, beat).items()
                                if beat == 1 or name != "cartridge"
                                for key in [name if beat == 1 else f"{name}_{beat:02}"]
                            }
                            for game in draft.games
                        ]
                elif path == "/api/jobs":
                    result = jobs.start(JobRequest.model_validate(data))
                elif path == "/api/resume":
                    result = jobs.resume(data["job_id"])
                elif path == "/api/shutdown":
                    if jobs.active():
                        raise ValueError(
                            "A generation job is running. Stop Studio after it finishes."
                        )
                    result = {"status": "stopping"}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise FileNotFoundError("Unknown route")
            self.respond(result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.failure(exc)

    def failure(self, exc):
        if isinstance(exc, PermissionError):
            self.respond({"error": str(exc)}, 403)
        elif isinstance(exc, FileNotFoundError):
            self.respond({"error": "Requested file or draft was not found"}, 404)
        elif isinstance(exc, (ValueError, KeyError, UnicodeError)):
            self.respond({"error": str(exc)}, 400)
        else:
            self.respond({"error": f"Studio request failed ({type(exc).__name__})"}, 500)

    def serve_file(self, path):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes"}
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            try:
                if not match or not any(match.groups()):
                    raise ValueError
                left, right = match.groups()
                start = int(left) if left else max(0, size - int(right))
                end = min(int(right), size - 1) if left and right else size - 1
                if not 0 <= start <= end < size:
                    raise ValueError
            except ValueError:
                self.headers_for(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                return
            status = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.headers_for(status, mime, length, extra)
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            stream.seek(start)
            while length:
                chunk = stream.read(min(length, 64 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)


def serve(project: Path, port: int):
    server = StudioServer(project, port)
    info = {
        "pid": os.getpid(),
        "port": server.server_address[1],
        "token": server.token,
        "project": str(server.store.project),
    }
    path = server.store.root / "server.json"
    write_json(path, info)
    print(f"Save Reel Studio: http://localhost:{info['port']}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if path.exists() and json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
