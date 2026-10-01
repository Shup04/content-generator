"""Filesystem layout and atomic JSON/artifact persistence for one run."""

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import TypeAdapter

from save_reel.models import Artifact, Reel, RunId, SaveId, utc_now


class RunStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    @classmethod
    def create(cls, runs_dir: Path, run_id: str | None = None) -> "RunStore":
        if run_id is None:
            run_id = f"{utc_now():%Y%m%dT%H%M%S%fZ}_{uuid4().hex[:8]}"
        TypeAdapter(RunId).validate_python(run_id)
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = runs_dir / run_id
        run_dir.mkdir()  # Never overwrite an existing run, including a partial one.
        return cls(run_dir)

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    def save_directory(self, save_id: SaveId) -> Path:
        TypeAdapter(SaveId).validate_python(save_id)
        path = self.run_dir / "saves" / save_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, relative_path: str, text: str, media_type: str = "text/plain") -> Artifact:
        return self.write_bytes(relative_path, text.encode("utf-8"), media_type)

    def write_bytes(self, relative_path: str, content: bytes, media_type: str) -> Artifact:
        artifact = Artifact(
            path=relative_path, media_type=media_type, sha256=hashlib.sha256(content).hexdigest()
        )
        self._write_atomic(self.run_dir / PurePosixPath(artifact.path), content)
        return artifact

    def save_manifest(self, reel: Reel) -> None:
        # Revalidate all nested state at the persistence boundary.
        validated = Reel.model_validate(reel)
        if validated.run_id != self.run_dir.name:
            raise ValueError("manifest run_id does not match the run directory")
        self._write_atomic(
            self.manifest_path, (validated.model_dump_json(indent=2) + "\n").encode()
        )

    def load_manifest(self) -> Reel:
        return Reel.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
