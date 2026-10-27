"""Raw customer data must not reach a Docker image, a Cloud Build upload or git."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRIVATE = [
    "data_offline/",
    "data_offline*.zip",
    "command_center_data/sample/",
    "command_center_data/auth/login_creds_plaintext.csv",
    "command_center_data/auth/brand_logins_summary.csv",
]


def _entries(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _ignored(path: str, entries: list[str]) -> bool:
    for e in entries:
        pat = e.rstrip("/")
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat + "/*") or path.startswith(pat + "/"):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(Path(path).name, pat[3:]):
            return True
    return False


def test_docker_and_cloud_build_skip_private_data():
    for name in (".dockerignore", ".gcloudignore"):
        entries = _entries(name)
        for p in PRIVATE:
            assert p in entries, f"{name} is missing {p}"
        for path in (
            "command_center_data/sample/AI_CUP_updated_Oct16.zip",
            "command_center_data/sample/customers.csv",
            "data_offline-20261010T164415Z-1-001.zip",
            "data_offline/customers.json",
        ):
            assert _ignored(path, entries), f"{name} would include {path}"
        assert not _ignored("command_center_data/auth/login_cred_db.csv", entries)
        assert not _ignored("command_center/main.py", entries)
        assert not _ignored("worker/main.py", entries)
        assert not _ignored("similarity/main.py", entries)


def test_git_skips_private_data():
    entries = _entries(".gitignore")
    for p in ("data_offline/", "data_offline*.zip", "command_center_data/sample/*.zip",
              "command_center_data/sample/customers.csv",
              "command_center_data/auth/login_creds_plaintext.csv"):
        assert p in entries


def test_command_center_image_copies_only_the_login_table():
    copies = re.findall(r"^COPY\s+(\S+)", (ROOT / "Dockerfile.command_center").read_text(), re.M)
    data = [c for c in copies if c.startswith("command_center_data")]
    assert data == ["command_center_data/auth/login_cred_db.csv"]
