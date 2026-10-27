"""Runs the web/ frontend suite (tests/frontend, node:test) so a plain `pytest` covers it too."""
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent / "frontend"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js 20+ not installed")
def test_frontend_suite():
    # File paths, not the directory: Node 22+ reads --test arguments as file globs.
    files = [str(p) for p in sorted(FRONTEND.glob("*.test.js"))]
    r = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
