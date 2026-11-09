"""Run the offline Studio acceptance and retain synthetic artifacts for inspection."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    missing += [name for name in ('pytest', 'httpx', 'dotenv')
                if importlib.util.find_spec(name) is None]
    if missing:
        print('Missing acceptance dependencies: ' + ', '.join(missing), file=sys.stderr)
        return 2
    target = Path(tempfile.mkdtemp(prefix='save-reel-offline-'))
    environment = os.environ.copy()
    for name in ('OPENAI_API_KEY', 'MINIMAX_API_KEY', 'ELEVENLABS_API_KEY'):
        environment.pop(name, None)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
                             '--tb=short', '--show-capture=no',
                             'tests/test_offline_acceptance.py', '--basetemp', str(target)],
                            cwd=root, env=environment)
    print(f'Synthetic acceptance artifacts retained at: {target}', flush=True)
    for path in target.rglob('acceptance-report.json'):
        print(f'Report: {path}')
    for path in target.rglob('offline-acceptance-reel/reel.mp4'):
        print(f'Finished MP4: {path}')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
