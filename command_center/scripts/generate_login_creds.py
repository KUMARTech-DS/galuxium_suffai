#!/usr/bin/env python3
"""Generate login_cred_db.csv (BQ) + login_creds_plaintext.csv (handoff).

Usage:
  python3 command_center/scripts/generate_login_creds.py
"""

from __future__ import annotations

import csv
import hashlib
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "command_center_data" / "sample"
OUT_DIR = REPO / "command_center_data" / "auth"

ADMIN_USER = "admin1"
ADMIN_PASS = "123@admin"


def sha256_hex(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def slug_brand(name: str) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "_" for ch in (name or "").strip())
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_") or "brand"


def rand_password(n: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def load_brands() -> list[str]:
    path = SAMPLE / "commodities.csv"
    brands: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            b = (row.get("brand") or "").strip()
            if b:
                brands.add(b)
    return sorted(brands)


def load_customers() -> list[dict[str, str]]:
    path = SAMPLE / "customers.csv"
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = (row.get("_id") or "").strip()
            if not cid:
                continue
            out.append(
                {
                    "customer_id": cid,
                    "name": (row.get("name") or "").strip() or cid[:8],
                }
            )
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    rows_plain: list[dict[str, str]] = []
    rows_bq: list[dict[str, str]] = []

    def add(
        *,
        username: str,
        password: str,
        role: str,
        brand_name: str = "",
        customer_id: str = "",
        display_name: str = "",
    ) -> None:
        rows_plain.append(
            {
                "username": username,
                "password": password,
                "role": role,
                "brand_name": brand_name,
                "customer_id": customer_id,
                "display_name": display_name or username,
            }
        )
        rows_bq.append(
            {
                "username": username,
                "password_hash": sha256_hex(password),
                "role": role,
                "brand_name": brand_name,
                "customer_id": customer_id,
                "display_name": display_name or username,
                "active": "true",
                "created_at": now,
            }
        )

    add(
        username=ADMIN_USER,
        password=ADMIN_PASS,
        role="admin",
        display_name="Central Admin",
    )

    brands = load_brands()
    for b in brands:
        slug = slug_brand(b)
        add(
            username=f"brand_{slug}",
            password=rand_password(10),
            role="brand",
            brand_name=b,
            display_name=b,
        )

    customers = load_customers()
    for c in customers:
        cid = c["customer_id"]
        add(
            username=f"cust_{cid[:8]}",
            password=rand_password(10),
            role="customer",
            customer_id=cid,
            display_name=c["name"],
        )

    bq_path = OUT_DIR / "login_cred_db.csv"
    plain_path = OUT_DIR / "login_creds_plaintext.csv"

    with bq_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "username",
                "password_hash",
                "role",
                "brand_name",
                "customer_id",
                "display_name",
                "active",
                "created_at",
            ],
        )
        w.writeheader()
        w.writerows(rows_bq)

    with plain_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "username",
                "password",
                "role",
                "brand_name",
                "customer_id",
                "display_name",
            ],
        )
        w.writeheader()
        w.writerows(rows_plain)

    # Also write a small brands-only summary for quick handoff
    brands_summary = OUT_DIR / "brand_logins_summary.csv"
    with brands_summary.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["username", "password", "brand_name", "role"]
        )
        w.writeheader()
        for r in rows_plain:
            if r["role"] == "brand":
                w.writerow(
                    {
                        "username": r["username"],
                        "password": r["password"],
                        "brand_name": r["brand_name"],
                        "role": r["role"],
                    }
                )

    print(f"Wrote {bq_path} ({len(rows_bq)} rows) — upload to BQ")
    print(f"Wrote {plain_path} — KEEP PRIVATE (gitignored)")
    print(f"Wrote {brands_summary}")
    print()
    print("=== Admin ===")
    print(f"  {ADMIN_USER} / {ADMIN_PASS}")
    print("=== Brands ===")
    for r in rows_plain:
        if r["role"] == "brand":
            print(f"  {r['username']} / {r['password']}  ({r['brand_name']})")
    print(f"=== Customers === {sum(1 for r in rows_plain if r['role']=='customer')} accounts in plaintext CSV")


if __name__ == "__main__":
    main()
