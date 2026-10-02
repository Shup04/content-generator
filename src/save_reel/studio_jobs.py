"""Durable, single-job execution. Workers outlive a disconnected browser."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from save_reel.studio_models import JobRequest
from save_reel.studio_progress import plan
from save_reel.studio_store import StudioStore, identifier, snapshot_prompts, write_json


def timestamp():
    return datetime.now(UTC).isoformat()


def worker_alive(job: dict) -> bool:
    pid = job.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists():
            command = cmdline.read_bytes().split(b"\0")
            return b"save_reel.studio_worker" in command and job["job_id"].encode() in command
        return True
    except (OSError, ProcessLookupError):
        return False


class StudioJobs:
    def __init__(self, store: StudioStore):
        self.store = store
        self.lock = threading.RLock()
        self.children = []

    def read(self, job_id: str) -> dict:
        path = self.store.jobs / identifier(job_id) / "job.json"
        job = json.loads(path.read_text())
        if job["status"] in ("running", "queued") and not worker_alive(job):
            job.update(status="interrupted", error="Worker stopped. Resume to reuse saved results.")
            write_json(path, job)
        return job

    def list(self):
        with self.lock:
            self.children = [p for p in self.children if p.poll() is None]
            paths = sorted(
                self.store.jobs.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            return [self.read(p.parent.name) for p in paths[:100]]

    def active(self):
        return next((j for j in self.list() if j["status"] in ("queued", "running")), None)

    def check_editable(self, draft_id):
        job = self.active()
        if job and job["draft_id"] == draft_id:
            raise ValueError("This draft is running. Wait for the job before editing it.")

    def start(self, request: JobRequest):
        with self.lock:
            if self.active():
                raise ValueError("A job is already running. Wait for it to finish.")
            draft = self.store.load(request.draft_id)
            if draft.revision != request.revision:
                raise ValueError("Draft has changed. Save or reload it before generating.")
            if request.action not in ("stories", "full") and not draft.games:
                raise ValueError("Generate or import four stories first")
            if (
                request.action == "full"
                and draft.provider == "mock"
                and (request.new_stories or not draft.games)
            ):
                raise ValueError("Mock mode is story-only. Choose OpenAI for a complete reel.")
            job_id = f"studio-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
            folder = self.store.jobs / job_id
            folder.mkdir()
            write_json(folder / "draft.json", draft.model_dump(mode="json"))
            snapshot_prompts(draft, folder / "prompts")
            job = {
                "job_id": job_id,
                "draft_id": draft.draft_id,
                "name": draft.name,
                "request": request.model_dump(mode="json"),
                "status": "queued",
                "phase": "Starting",
                "created_at": timestamp(),
                "finished_at": None,
                "error": None,
                "pid": None,
                "outputs": {},
                "progress": plan(request, draft),
            }
            self._spawn(folder, job)
            return job

    def resume(self, job_id):
        with self.lock:
            if self.active():
                raise ValueError("A job is already running")
            job = self.read(job_id)
            if job["status"] == "completed":
                raise ValueError("This job is already complete")
            snapshot = json.loads((self.store.jobs / job_id / "draft.json").read_text())
            if self.store.load(job["draft_id"]).revision != snapshot["revision"]:
                raise ValueError("The draft was edited after this job. Start a new job instead.")
            job.update(status="queued", error=None, finished_at=None)
            self._spawn(self.store.jobs / identifier(job_id), job)
            return job

    def _spawn(self, folder, job):
        write_json(folder / "job.json", job)
        with (folder / "job.log").open("ab") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "save_reel.studio_worker",
                    str(self.store.project),
                    job["job_id"],
                ],
                cwd=self.store.project,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.children.append(child)
        # The worker waits for this PID handshake before touching the job file.
        job["pid"] = child.pid
        write_json(folder / "job.json", job)
