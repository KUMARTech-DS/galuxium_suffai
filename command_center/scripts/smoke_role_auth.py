#!/usr/bin/env python3
"""Smoke admin/brand/customer login + optional GET /v1/metrics.

Env:
  AUTH_SOURCE=csv          — use local login_cred_db.csv (default if no CC_URL)
  CC_URL                   — if set, POST /v1/auth/login + GET /v1/metrics over HTTP
  LOGIN_PLAINTEXT_CSV      — override plaintext handoff path
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAIN = Path(os.getenv("LOGIN_PLAINTEXT_CSV") or ROOT / "command_center_data/auth/login_creds_plaintext.csv")


def _plain_rows() -> list[dict[str, str]]:
    if not PLAIN.is_file():
        print(f"FAIL: missing {PLAIN} — run: python3 command_center/scripts/generate_login_creds.py")
        sys.exit(2)
    return list(csv.DictReader(PLAIN.open(encoding="utf-8")))


def _one(role: str, rows: list[dict[str, str]]) -> dict[str, str]:
    for r in rows:
        if (r.get("role") or "").strip() == role:
            return r
    print(f"FAIL: no {role} row in {PLAIN}")
    sys.exit(2)


def smoke_csv() -> None:
    os.environ.setdefault("AUTH_SOURCE", "csv")
    sys.path.insert(0, str(ROOT / "command_center"))
    from auth_acl import authenticate, destroy_session  # type: ignore

    rows = _plain_rows()
    for role in ("admin", "brand", "customer"):
        r = _one(role, rows)
        sess = authenticate(r["username"], r["password"])
        assert sess["role"] == role, sess
        print(f"OK csv login {role} user={sess['username']} tabs={sess['allowed_tabs']}")
        destroy_session(sess["token"])


def _http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return exc.code, payload


def smoke_http(cc_url: str) -> None:
    base = cc_url.rstrip("/")
    rows = _plain_rows()
    for role in ("admin", "brand", "customer"):
        r = _one(role, rows)
        status, data = _http_json(
            "POST",
            f"{base}/v1/auth/login",
            {"username": r["username"], "password": r["password"]},
        )
        if status != 200 or not data.get("token"):
            print(f"FAIL http login {role} status={status} detail={data.get('detail')!r}")
            sys.exit(1)
        token = data["token"]
        print(f"OK http login {role} user={data.get('username')} tabs={data.get('allowed_tabs')}")

        m_status, m_data = _http_json(
            "GET",
            f"{base}/v1/metrics",
            headers={"Authorization": f"Bearer {token}", "X-Session-Token": token},
        )
        if m_status != 200:
            detail = m_data.get("detail", m_data)
            print(f"FAIL metrics {role} status={m_status} detail={json.dumps(detail)[:500]}")
            sys.exit(1)
        if not isinstance(m_data, dict) or "filters" not in m_data:
            print(f"FAIL metrics {role}: unexpected payload keys={list(m_data)[:12]}")
            sys.exit(1)
        print(f"OK metrics {role} status=200 pins={len((m_data.get('filters') or {}).get('pin_codes') or [])}")


def main() -> None:
    cc = (os.getenv("CC_URL") or "").strip()
    if cc:
        smoke_http(cc)
    else:
        smoke_csv()
    print("smoke_role_auth: all checks passed")


if __name__ == "__main__":
    main()
