"""Gemini with Google Search grounding (Vertex AI via the google-genai SDK).

Grounded answers must be shown with their sources and the Google search suggestions chip
(search_entry_html), per the Grounding with Google Search terms.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("command_center.gemini_grounded")


class GroundingUnavailable(RuntimeError):
    """Grounded Gemini is switched off, misconfigured or failed."""


@dataclass
class GroundedAnswer:
    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    search_entry_html: Optional[str] = None
    queries: list[str] = field(default_factory=list)
    model: str = ""


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _env_bool("ENABLE_VERTEX", True) and _env_bool("GEO_GROUNDING_ENABLED", True)


def model_name() -> str:
    return os.getenv("GEO_GROUNDING_MODEL", "gemini-2.5-flash")


# Test hook: replaces the Vertex call; receives the prompt, returns a GroundedAnswer.
_override: Optional[Callable[[str], GroundedAnswer]] = None


def _extract(resp: Any, model: str) -> GroundedAnswer:
    text = getattr(resp, "text", None) or ""
    sources: list[dict[str, str]] = []
    entry_html = None
    queries: list[str] = []
    for cand in getattr(resp, "candidates", None) or []:
        if not text:
            parts = getattr(getattr(cand, "content", None), "parts", None) or []
            text = "\n".join(p.text for p in parts if getattr(p, "text", None))
        gm = getattr(cand, "grounding_metadata", None)
        if not gm:
            continue
        for chunk in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri and all(s["url"] != uri for s in sources):
                sources.append({"title": str(getattr(web, "title", "") or uri)[:160], "url": str(uri)})
        sep = getattr(gm, "search_entry_point", None)
        if sep is not None and getattr(sep, "rendered_content", None):
            entry_html = str(sep.rendered_content)
        queries += [str(q) for q in (getattr(gm, "web_search_queries", None) or [])]
        break
    return GroundedAnswer(text=text.strip(), sources=sources[:12], search_entry_html=entry_html,
                          queries=queries[:8], model=model)


def ask(prompt: str, *, request_id: str = "-") -> GroundedAnswer:
    """One grounded generation. Raises GroundingUnavailable on any failure."""
    if _override is not None:
        return _override(prompt)
    if not enabled():
        raise GroundingUnavailable("grounding disabled")
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        raise GroundingUnavailable("GOOGLE_CLOUD_PROJECT not set")
    model = model_name()
    location = os.getenv("GEO_GROUNDING_LOCATION", "global")
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise GroundingUnavailable(f"google-genai not installed: {exc}") from exc
    try:
        client = genai.Client(vertexai=True, project=project, location=location)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )
    except Exception as exc:
        log.exception("request_id=%s grounded call failed model=%s location=%s", request_id, model, location)
        raise GroundingUnavailable(f"grounded call failed: {exc}") from exc
    answer = _extract(resp, model)
    if not answer.text:
        raise GroundingUnavailable("empty grounded response")
    log.info("request_id=%s grounded ok model=%s sources=%s", request_id, model, len(answer.sources))
    return answer


def parse_json(text: str) -> Any:
    """Grounding cannot force a JSON response format, so pull the first JSON value out of the text."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in grounded response")
    start = min(starts)
    end = raw.rfind("}" if raw[start] == "{" else "]")
    if end <= start:
        raise ValueError("unterminated JSON in grounded response")
    return json.loads(raw[start : end + 1])
