"""
Brand SKU vs. garments photographed in PINs (read-only).

Used by Command Center Ads Lab ranking. Command Center sends each PIN's garments (BigQuery garment_items,
counted at the customer's current PIN: command_center/pin_garments.corpus); this service only adds their
embeddings from Cloud SQL unique_garments. Never writes: brand catalogue images are not customer photos.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from array import array
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from similarity import db
from similarity.embed import embed_image
from similarity.gcs_io import read_image
from similarity.settings import settings

log = logging.getLogger("similarity.compare")

PIN_RE = re.compile(r"^\d{6}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# (pin, garment_id, photos, unit_embedding)
Candidate = Tuple[str, str, int, Sequence[float]]
# garment ids -> {garment_id: (embedding, embedding_model)}
EmbeddingLoader = Callable[[Sequence[str]], Dict[str, Tuple[List[float], Optional[str]]]]

_cache_lock = threading.Lock()
# garment_id -> (expires_at, unit embedding, embedding_model); a garment's embedding never changes.
_cache: Dict[str, Tuple[float, Sequence[float], Optional[str]]] = {}


def normalize_pins(pin_codes: Sequence[Any]) -> List[str]:
    pins: List[str] = []
    for p in pin_codes or []:
        s = str(p or "").strip()
        if not s:
            continue
        if not PIN_RE.match(s):
            raise ValueError(f"pin_code must be 6 digits, got: {s!r}")
        if s not in pins:
            pins.append(s)
    if not pins:
        raise ValueError("pin_codes required (one or more)")
    if len(pins) > settings.COMPARE_MAX_PINS:
        raise ValueError(f"at most {settings.COMPARE_MAX_PINS} pin_codes")
    return pins


def normalize_corpus(
    pins: Sequence[str], candidates: Any, pin_totals: Optional[Mapping[str, Any]] = None
) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, int]]:
    """Validated candidates (PIN -> [(garment_id, photos)]) and PIN totals from the request.

    A PIN without a total gets the photos of its listed garments.
    """
    if not isinstance(candidates, Mapping):
        raise ValueError("candidates required: {pin_code: [[garment_id, photos], ...]}")
    extra = sorted(str(p) for p in candidates if str(p) not in pins)
    if extra:
        raise ValueError(f"candidates for PINs not in pin_codes: {', '.join(extra[:5])}")
    totals_in = pin_totals if isinstance(pin_totals, Mapping) else {}
    out: Dict[str, List[Tuple[str, int]]] = {}
    totals: Dict[str, int] = {}
    n = 0
    for pin in pins:
        rows = candidates.get(pin) or []
        if not isinstance(rows, (list, tuple)):
            raise ValueError(f"candidates[{pin}] must be a list")
        seen: List[Tuple[str, int]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("each candidate is [garment_id, photos]")
            gid = str(row[0] or "").strip().lower()
            if not UUID_RE.match(gid):
                raise ValueError(f"garment_id must be a UUID, got: {str(row[0])[:40]!r}")
            try:
                count = max(1, int(row[1]))
            except (TypeError, ValueError) as exc:
                raise ValueError("candidate photos must be a whole number") from exc
            seen.append((gid, count))
        n += len(seen)
        if n > settings.COMPARE_MAX_CANDIDATES:
            raise ValueError(f"at most {settings.COMPARE_MAX_CANDIDATES} candidates")
        out[pin] = seen
        try:
            totals[pin] = max(0, int(totals_in[pin])) if pin in totals_in else sum(c for _, c in seen)
        except (TypeError, ValueError) as exc:
            raise ValueError("pin_totals must be whole numbers") from exc
    return out, totals


def _unit(vec: Sequence[float]) -> Sequence[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return []
    return array("f", (x / norm for x in vec))


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def unit_embeddings(
    ids: Sequence[str], *, loader: Optional[EmbeddingLoader] = None
) -> Dict[str, Tuple[Sequence[float], Optional[str]]]:
    """garment_id -> (unit embedding, model) for the ids that have one; cached COMPARE_CACHE_SEC."""
    ttl = settings.COMPARE_CACHE_SEC
    now = time.monotonic()
    out: Dict[str, Tuple[Sequence[float], Optional[str]]] = {}
    missing: List[str] = []
    with _cache_lock:
        for gid in ids:
            hit = _cache.get(gid) if ttl > 0 else None
            if hit and hit[0] > now:
                out[gid] = (hit[1], hit[2])
            else:
                missing.append(gid)
    if missing:
        loaded = (loader or db.load_embeddings)(missing)
        fresh = {gid: (_unit(emb), model) for gid, (emb, model) in loaded.items()}
        out.update(fresh)
        if ttl > 0:
            with _cache_lock:
                if len(_cache) + len(fresh) > settings.COMPARE_EMBED_CACHE_MAX:
                    _cache.clear()
                for gid, (unit, model) in fresh.items():
                    _cache[gid] = (now + ttl, unit, model)
    return out


def load_candidates(
    pins: Sequence[str],
    corpus: Mapping[str, Sequence[Tuple[str, int]]],
    model: str,
    dim: int,
    *,
    loader: Optional[EmbeddingLoader] = None,
) -> Tuple[List[Candidate], int]:
    """The corpus garments with an embedding of the same model and size, and how many have none."""
    ids = sorted({gid for p in pins for gid, _ in corpus.get(p) or []})
    embs = unit_embeddings(ids, loader=loader)
    cands: List[Candidate] = []
    missing = skipped = 0
    for pin in pins:
        for gid, count in corpus.get(pin) or []:
            hit = embs.get(gid)
            if hit is None:
                missing += 1
                continue
            unit, emb_model = hit
            if len(unit) != dim or (emb_model and emb_model != model):
                skipped += 1
                continue
            cands.append((pin, gid, int(count), unit))
    if skipped:
        log.warning("compare candidates skipped (model/dim mismatch) n=%s model=%s dim=%s", skipped, model, dim)
    if missing:
        log.warning("compare candidates without a Cloud SQL embedding n=%s", missing)
    return cands, missing


def score_pins(
    query: Sequence[float],
    pins: Sequence[str],
    candidates: Sequence[Candidate],
    totals: Dict[str, int],
    *,
    topk: int,
) -> Dict[str, Any]:
    """
    pin_fit: photo-weighted mean cosine (floored at 0) of the top-k most similar garments.
    aggregate_fit: PIN fits weighted by each PIN's total photos (plain mean if none).
    """
    q = _unit(query)
    by_pin: Dict[str, List[Tuple[float, str, int]]] = {p: [] for p in pins}
    for pin, gid, count, unit in candidates:
        if pin in by_pin:
            by_pin[pin].append((max(0.0, _dot(q, unit)) if len(q) else 0.0, gid, count))
    per_pin: Dict[str, Any] = {}
    for pin in pins:
        scored = sorted(by_pin[pin], key=lambda t: t[0], reverse=True)[:topk]
        weight = sum(max(1, c) for _, _, c in scored)
        fit = sum(s * max(1, c) for s, _, c in scored) / weight if weight else 0.0
        per_pin[pin] = {
            "pin_fit": round(fit, 4),
            "total_observations": int(totals.get(pin, 0)),
            "candidates": len(by_pin[pin]),
            "top_matches": [
                {"garment_id": gid, "similarity": round(s, 4), "observation_count": c}
                for s, gid, c in scored
            ],
        }
    weighted = [(d["pin_fit"], d["total_observations"]) for d in per_pin.values() if d["candidates"]]
    total_w = sum(w for _, w in weighted)
    if total_w > 0:
        aggregate = sum(f * w for f, w in weighted) / total_w
    elif weighted:
        aggregate = sum(f for f, _ in weighted) / len(weighted)
    else:
        aggregate = 0.0
    return {"aggregate_fit": round(aggregate, 4), "per_pin": per_pin}


def compare_pins(
    *,
    gcs_object_key: str,
    gcs_bucket: Optional[str],
    pin_codes: Sequence[Any],
    candidates: Any,
    pin_totals: Optional[Mapping[str, Any]] = None,
    topk: int = 5,
    include_embedding: bool = True,
) -> Dict[str, Any]:
    pins = normalize_pins(pin_codes)
    corpus, totals = normalize_corpus(pins, candidates, pin_totals)
    topk = max(1, min(50, int(topk or 5)))
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    t0 = time.monotonic()
    image = read_image(gcs_object_key, bucket=gcs_bucket)
    embedding, model = embed_image(image)
    cands, missing = load_candidates(pins, corpus, model, len(embedding))
    out = score_pins(embedding, pins, cands, totals, topk=topk)
    log.info(
        "compare_pins key=%s pins=%s candidates=%s missing=%s fit=%.4f ms=%d",
        image.object_key, len(pins), len(cands), missing, out["aggregate_fit"],
        int((time.monotonic() - t0) * 1000),
    )
    out.update(
        {
            "status": "ok",
            "gcs_object_key": image.object_key,
            "pin_codes": pins,
            "topk": topk,
            "embedding_model": model,
            "embedding_dim": len(embedding),
            "missing_embeddings": missing,
        }
    )
    if include_embedding:
        out["embedding"] = list(embedding)
    return out
