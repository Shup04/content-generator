"""Start/stop a local background Studio, or serve it in the foreground."""

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from save_reel.studio_store import StudioStore


def add_studio_command(commands):
    studio = commands.add_parser(
        "studio", help="Local browser interface for editing and generation"
    )
    studio.add_argument(
        "operation", choices=("start", "stop", "status", "serve"), default="start", nargs="?"
    )
    studio.add_argument("--port", type=int, default=8765)
    studio.add_argument("--project-dir", type=Path, default=Path.cwd())


def connection(store):
    path = store.root / "server.json"
    if not path.exists():
        return None
    info = json.loads(path.read_text())
    try:
        with urlopen(f"http://127.0.0.1:{info['port']}/api/bootstrap", timeout=2) as response:
            data = json.load(response)
        if data["project"] == str(store.project) and data["token"] == info["token"]:
            return info
    except (URLError, OSError, ValueError, KeyError):
        pass
    return None


def run_studio_command(args):
    store = StudioStore(args.project_dir)
    if not 1024 <= args.port <= 65535:
        raise ValueError("Choose a port between 1024 and 65535")
    existing = connection(store)
    if args.operation == "status":
        print(
            f"Studio is running: http://localhost:{existing['port']}"
            if existing
            else "Studio is stopped"
        )
    elif args.operation == "stop":
        if not existing:
            print("Studio is already stopped")
            return
        request = Request(
            f"http://127.0.0.1:{existing['port']}/api/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Studio-Token": existing["token"]},
        )
        try:
            with urlopen(request, timeout=5) as response:
                json.load(response)
        except URLError as exc:
            if hasattr(exc, "read"):
                raise ValueError(
                    json.loads(exc.read()).get("error", "Cannot stop Studio")
                ) from None
            raise ValueError("Cannot connect to Studio") from None
        print("Studio is stopping")
    elif existing:
        print(f"Studio is already running: http://localhost:{existing['port']}")
    elif args.operation == "serve":
        from save_reel.studio_server import serve

        serve(store.project, args.port)
    else:
        log_path = store.root / "server.log"
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "save_reel",
                    "studio",
                    "serve",
                    "--project-dir",
                    str(store.project),
                    "--port",
                    str(args.port),
                ],
                cwd=store.project,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        for _ in range(60):
            info = connection(store)
            if info:
                print(f"Studio is running: http://localhost:{info['port']}")
                return
            if child.poll() is not None:
                break
            time.sleep(0.1)
        raise ValueError(f"Studio did not start. Check {log_path}")
