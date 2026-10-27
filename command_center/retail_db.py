"""Cloud SQL access for the retail suite: one transaction per `tx()`, dict rows, %s placeholders.

Tests install a SQLite connection factory with `use_sqlite()` and run the same SQL and the same
db/migrations/012_retail_ops.sql (+ 013 catalog images) schema, so the SQL here sticks to what both
engines accept. brand_skus ALTER lines in 013 are Cloud SQL only and are skipped on SQLite.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

log = logging.getLogger("command_center.retail_db")

IST = timezone(timedelta(hours=5, minutes=30))
_MIG_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"
MIGRATIONS = (
    _MIG_DIR / "012_retail_ops.sql",
    _MIG_DIR / "013_catalog_images.sql",
)
# Back-compat alias used by a few tests.
MIGRATION = MIGRATIONS[0]

# Test hooks: a zero-argument SQLite connection factory, and a clock returning an aware datetime.
_factory: Optional[Callable[[], sqlite3.Connection]] = None
_clock: Optional[Callable[[], datetime]] = None


class RetailError(ValueError):
    """A request the user can fix (4xx); the message is shown as-is. `errors` lists per-row problems."""

    def __init__(self, message: str, status: int = 400, errors: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class RetailUnavailable(RuntimeError):
    """Storage is not configured or failed (503, masked behind a reference)."""


def configured() -> bool:
    return _factory is not None or bool((os.getenv("DATABASE_URL") or "").strip())


def use_sqlite(path: str) -> None:
    """Point the retail suite at a SQLite file and create the schema there (tests)."""
    global _factory

    def factory() -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    conn = factory()
    try:
        for mig in MIGRATIONS:
            sql = mig.read_text(encoding="utf-8")
            # brand_skus lives in the campaign Cloud SQL schema, not the retail SQLite file.
            if mig.name.startswith("013"):
                sql = "\n".join(
                    ln for ln in sql.splitlines()
                    if "brand_skus" not in ln.lower()
                )
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    _factory = factory


def reset() -> None:
    global _factory, _clock
    _factory = None
    _clock = None


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now(timezone.utc)


def now_iso() -> str:
    return now().astimezone(timezone.utc).isoformat()


def today_ist() -> str:
    return now().astimezone(IST).date().isoformat()


def ist_day_start_utc(day: str) -> str:
    """UTC ISO timestamp of 00:00 IST on `day` (YYYY-MM-DD); compares correctly with stored UTC timestamps."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=IST)
    return start.astimezone(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True, default=str)


def loads(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat() if v.tzinfo else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


class Tx:
    """One open transaction. Every statement uses %s placeholders."""

    def __init__(self, conn: Any, sqlite: bool):
        self._conn = conn
        self._sqlite = sqlite
        self._cur = conn.cursor()

    def _exec(self, sql: str, params: Sequence[Any]) -> Any:
        if self._sqlite:
            sql = sql.replace("%s", "?")
        self._cur.execute(sql, tuple(params))
        return self._cur

    def run(self, sql: str, params: Sequence[Any] = ()) -> int:
        return int(self._exec(sql, params).rowcount or 0)

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._exec(sql, params)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [{c: _value(v) for c, v in zip(cols, row)} for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None


def _is_integrity(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return type(exc).__name__ in {"IntegrityError", "UniqueViolation", "ForeignKeyViolation", "CheckViolation"}


@contextmanager
def tx() -> Iterator[Tx]:
    if _factory is not None:
        try:
            conn, sqlite = _factory(), True
        except Exception as exc:
            raise RetailUnavailable(f"SQLite connect failed: {type(exc).__name__}") from exc
    else:
        dsn = (os.getenv("DATABASE_URL") or "").strip()
        if not dsn:
            raise RetailUnavailable("DATABASE_URL not set")
        try:
            import psycopg2  # type: ignore

            conn = psycopg2.connect(dsn, connect_timeout=10)
        except Exception as exc:
            raise RetailUnavailable(f"Cloud SQL connect failed: {type(exc).__name__}") from exc
        sqlite = False
    try:
        yield Tx(conn, sqlite)
        conn.commit()
    except (RetailError, RetailUnavailable):
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        if _is_integrity(exc):
            raise RetailError("That conflicts with an existing record. Refresh and try again.", 409) from exc
        if isinstance(exc, (sqlite3.Error,)) or type(exc).__module__.startswith("psycopg2"):
            raise RetailUnavailable(f"Cloud SQL statement failed: {type(exc).__name__}") from exc
        raise
    finally:
        conn.close()


def next_number(t: Tx, scope_key: str, doc_type: str, period: str) -> int:
    """Next gap-free number for (scope, type, period). Runs inside the caller's transaction, so a
    rolled-back document gives its number back."""
    n = t.scalar(
        "INSERT INTO retail_doc_sequences (scope_key, doc_type, period, next_no) VALUES (%s, %s, %s, 1) "
        "ON CONFLICT (scope_key, doc_type, period) DO UPDATE SET next_no = retail_doc_sequences.next_no + 1 "
        "RETURNING next_no",
        (scope_key, doc_type, period),
    )
    return int(n)


def audit(t: Tx, brand_key: str, actor: str, action: str, entity: str, entity_id: Optional[str],
          detail: Optional[dict[str, Any]] = None) -> None:
    t.run(
        "INSERT INTO retail_audit (event_id, brand_key, actor, action, entity, entity_id, detail, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (new_id(), brand_key, actor, action, entity, entity_id, dumps(detail or {}), now_iso()),
    )
