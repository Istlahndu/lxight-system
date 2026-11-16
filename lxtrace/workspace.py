"""Keep demo build and test scratch files inside this project."""
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def temporary_root():
    path = ROOT / "artifacts" / "tmp"
    # Refuse a redirected scratch directory before creating anything through it.
    path.resolve().relative_to(ROOT.resolve())
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_environment(extra=None):
    env = dict(os.environ)
    if extra:
        env.update(extra)
    scratch = str(temporary_root())
    env.update(TMPDIR=scratch, TEMP=scratch, TMP=scratch,
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPYCACHEPREFIX", None)
    return env


def temporary_directory():
    return tempfile.TemporaryDirectory(prefix="lxtrace-", dir=str(temporary_root()))
