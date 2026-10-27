"""Warehouse and store staff logins, kept in BigQuery login_cred_db with every other login.

A staff login carries its brand (brand_name) and the code of the one location it works at
(location_code, added by scripts/migrate_bq_retail.sql). Only the sha256 of a password is stored;
generated passwords are returned once and never logged.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Callable, Optional

import auth_acl
import retail_db as db
import retail_stock as stock
import retail_valid as v
from retail_db import RetailError, RetailUnavailable
from retail_scope import STAFF_ROLES, Scope

log = logging.getLogger("command_center.retail_staff")

ROLE_KIND = {"warehouse_staff": "warehouse", "store_staff": "store"}
_BRAND_KEY_SQL = "REGEXP_REPLACE(LOWER(TRIM(IFNULL(brand_name, ''))), r'[^a-z0-9]', '')"

Param = tuple[str, str, Any]
# Test hook: (sql, params) -> rows instead of BigQuery.
_runner: Optional[Callable[[str, list[Param]], list[dict[str, Any]]]] = None


def _table() -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        raise RetailUnavailable("GOOGLE_CLOUD_PROJECT not set")
    return f"`{project}.{os.getenv('BQ_DATASET', 'suffai_ops')}.login_cred_db`"


def _run(sql: str, params: list[Param]) -> list[dict[str, Any]]:
    if _runner is not None:
        return _runner(sql, params)
    try:
        from google.cloud import bigquery  # type: ignore

        client = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT"))
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter(n, t, x) for n, t, x in params])
        return [dict(r) for r in client.query(sql, job_config=cfg).result()]
    except Exception as exc:
        hint = " (run scripts/migrate_bq_retail.sql)" if "location_code" in str(exc) else ""
        raise RetailUnavailable(f"login_cred_db query failed: {type(exc).__name__}{hint}") from exc


def list_staff(scope: Scope) -> list[dict[str, Any]]:
    scope.require("staff.manage")
    rows = _run(
        f"SELECT username, role, display_name, location_code, IFNULL(active, TRUE) AS active, created_at "
        f"FROM {_table()} WHERE role IN ('warehouse_staff', 'store_staff') AND {_BRAND_KEY_SQL} = @brand_key "
        "ORDER BY location_code, username",
        [("brand_key", "STRING", scope.brand_key)])
    return [{"username": r["username"], "role": r["role"], "display_name": r.get("display_name"),
             "location_code": r.get("location_code"), "active": bool(r.get("active")),
             "created_at": str(r["created_at"]) if r.get("created_at") else None} for r in rows]


def create_staff(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("staff.manage")
    role = str(data.get("role") or "").strip()
    if role not in STAFF_ROLES:
        raise RetailError("Pick warehouse staff or store staff.")
    username = v.username(data.get("username"))
    display = v.text(data.get("display_name"), "Name", hi=80)
    code = v.location_code(data.get("location_code"))
    with db.tx() as t:
        loc = stock.location_by_code(t, scope.brand_key, code)
    if not loc or not loc["active"]:
        raise RetailError(f"No open location with code {code}.", 404)
    if loc["kind"] != ROLE_KIND[role]:
        raise RetailError(f"{loc['name']} is a {loc['kind']}; pick a {ROLE_KIND[role]} for this role.")
    table = _table()
    if _run(f"SELECT username FROM {table} WHERE username = @username LIMIT 1", [("username", "STRING", username)]):
        raise RetailError(f"Username {username} is taken. Try another.", 409)
    password = secrets.token_urlsafe(9)
    _run(
        f"INSERT INTO {table} (username, password_hash, role, brand_name, customer_id, display_name, location_code, "
        "active, created_at) VALUES (@username, @password_hash, @role, @brand_name, NULL, @display_name, @location_code, "
        "TRUE, CURRENT_TIMESTAMP())",
        [("username", "STRING", username), ("password_hash", "STRING", auth_acl.sha256_hex(password)),
         ("role", "STRING", role), ("brand_name", "STRING", scope.brand_name), ("display_name", "STRING", display),
         ("location_code", "STRING", code)])
    with db.tx() as t:
        db.audit(t, scope.brand_key, scope.actor, "create", "staff_login", username, {"role": role, "location": code})
    log.info("brand=%s actor=%s created staff login=%s role=%s location=%s", scope.brand_key, scope.actor, username,
             role, code)
    return {"username": username, "password": password, "role": role, "location_code": code, "display_name": display}


def _owned(scope: Scope, username: str) -> str:
    username = v.username(username)
    rows = _run(f"SELECT username FROM {_table()} WHERE username = @username AND role IN ('warehouse_staff', "
                f"'store_staff') AND {_BRAND_KEY_SQL} = @brand_key LIMIT 1",
                [("username", "STRING", username), ("brand_key", "STRING", scope.brand_key)])
    if not rows:
        raise RetailError("Staff login not found.", 404)
    return username


def set_active(scope: Scope, username: str, active: bool) -> dict[str, Any]:
    scope.require("staff.manage")
    username = _owned(scope, username)
    _run(f"UPDATE {_table()} SET active = @active WHERE username = @username",
         [("active", "BOOL", bool(active)), ("username", "STRING", username)])
    ended = 0 if active else auth_acl.destroy_user_sessions(username)
    with db.tx() as t:
        db.audit(t, scope.brand_key, scope.actor, "activate" if active else "deactivate", "staff_login", username, {})
    log.info("brand=%s actor=%s staff login=%s active=%s", scope.brand_key, scope.actor, username, active)
    return {"username": username, "active": bool(active), "sessions_ended": ended}


def reset_password(scope: Scope, username: str) -> dict[str, Any]:
    scope.require("staff.manage")
    username = _owned(scope, username)
    password = secrets.token_urlsafe(9)
    _run(f"UPDATE {_table()} SET password_hash = @password_hash WHERE username = @username",
         [("password_hash", "STRING", auth_acl.sha256_hex(password)), ("username", "STRING", username)])
    auth_acl.destroy_user_sessions(username)
    with db.tx() as t:
        db.audit(t, scope.brand_key, scope.actor, "reset_password", "staff_login", username, {})
    log.info("brand=%s actor=%s reset password for staff login=%s", scope.brand_key, scope.actor, username)
    return {"username": username, "password": password}
