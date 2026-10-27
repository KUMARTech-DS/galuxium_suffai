"""Notice BigQuery writes made outside the Command Center (extraction, similarity, CSV loads, scripts).

Every request that reads cached metrics or Admin data calls poke(). At most once per BQ_FRESHNESS_SEC
(default 30 s) a background check reads the last-modified time of the tables the dashboards read; when
any of them moved, the registered callbacks clear the caches, so new data shows within about 30 s
instead of after the cache TTLs. BQ_FRESHNESS_SEC=0 turns the check off.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("command_center.bq_freshness")

TABLES = ("customers", "orders", "commodities", "garment_similarity_observations", "pin_areas")

# Test hooks: table name -> modified value (None = unknown); _sync runs the check inline.
_reader: Optional[Callable[[str], Any]] = None
_sync = False

_lock = threading.Lock()
_state: dict[str, Any] = {"checked": 0.0, "running": False, "modified": {}}
_callbacks: list[Callable[[], None]] = []
_client: Any = None


def interval() -> float:
    try:
        return float(os.getenv("BQ_FRESHNESS_SEC", "30"))
    except ValueError:
        return 30.0


def on_change(fn: Callable[[], None]) -> None:
    if fn not in _callbacks:
        _callbacks.append(fn)


def reset() -> None:
    with _lock:
        _state.update(checked=0.0, running=False, modified={})


def _modified(table: str) -> Any:
    if _reader is not None:
        return _reader(table)
    global _client
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        return None
    from google.cloud import bigquery  # type: ignore

    if _client is None:
        _client = bigquery.Client(project=project)
    try:
        return _client.get_table(f"{project}.{os.getenv('BQ_DATASET', 'suffai_ops')}.{table}").modified
    except Exception as exc:
        if "notfound" in type(exc).__name__.lower() or "not found" in str(exc).lower():
            return None  # e.g. pin_areas before the migration
        raise


def check() -> list[str]:
    """Read the tables' modified times; returns the tables that changed since the last check.

    The first check only records a baseline. Callbacks run once per check with any change.
    """
    try:
        current = {t: _modified(t) for t in TABLES}
    except Exception as exc:
        log.warning("table freshness check failed: %s", type(exc).__name__)
        return []
    with _lock:
        prev = _state["modified"]
        changed = [t for t, m in current.items() if m is not None and t in prev and prev[t] != m]
        _state["modified"] = {**prev, **{t: m for t, m in current.items() if m is not None}}
    if changed:
        log.info("BigQuery tables changed %s; clearing metrics and Admin caches", ",".join(changed))
        for fn in list(_callbacks):
            try:
                fn()
            except Exception:
                log.warning("cache clear callback failed", exc_info=True)
    return changed


def _run() -> None:
    try:
        check()
    finally:
        with _lock:
            _state["running"] = False


def poke() -> None:
    """Start a check when one is due; never blocks the request (the next request sees fresh data)."""
    every = interval()
    if every <= 0:
        return
    now = time.monotonic()
    with _lock:
        if _state["running"] or (_state["checked"] and now - _state["checked"] < every):
            return
        _state.update(checked=now, running=True)
    if _sync:
        _run()
    else:
        threading.Thread(target=_run, daemon=True, name="bq-freshness").start()
