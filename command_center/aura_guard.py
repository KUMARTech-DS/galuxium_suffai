"""AURA safety layer: input screening, untrusted-data wrapping, output scrubbing,
masked errors, rate limiting, confirmation tokens and chat upload staging.

Everything here is deterministic (no model calls) so it can run before and after
every Gemini turn.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger("command_center.aura_guard")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


AURA_MAX_MESSAGE_CHARS = _env_int("AURA_MAX_MESSAGE_CHARS", 2000)
AURA_RATE_PER_MIN = _env_int("AURA_RATE_PER_MIN", 20)
AURA_MAX_UPLOADS = _env_int("AURA_MAX_UPLOADS", 10)
AURA_MAX_UPLOAD_MB = _env_int("AURA_MAX_UPLOAD_MB", 5)
AURA_CONFIRM_TTL_SEC = _env_int("AURA_CONFIRM_TTL_SEC", 300)
AURA_UPLOAD_TTL_SEC = _env_int("AURA_UPLOAD_TTL_SEC", 1800)
AURA_UNTRUSTED_MAX_CHARS = _env_int("AURA_UNTRUSTED_MAX_CHARS", 12000)


# Keys that must never be forwarded to Gemini (PII / raw identity / precise personal geo)
PII_KEY_DENY = frozenset(
    {
        "name",
        "display_name",
        "username",
        "email",
        "phone",
        "number",
        "address",
        "customer_id",
        "user_uuid",
        "_id",
        "id",
        "token",
        "password",
        "password_hash",
        "lat",
        "lon",
        "centroid_lat",
        "centroid_lon",
    }
)
_PII_KEY_SUBSTR = ("email", "phone", "address", "password", "token", "secret")

# Internal identifiers / infrastructure fields the model never needs.
_INTERNAL_KEY_DENY = frozenset(
    {
        "campaign_id",
        "job_id",
        "sku_uuid",
        "selected_sku_ids",
        "hero_sku_id",
        "gcs_bucket",
        "gcs_object_key",
        "object_key",
        "image_url",
        "signed_url",
        "raw_result",
        "embedding",
        "per_pin",
        "error_message",
        "extract_error",
        "compare_error",
        "request_id",
        "source_context",
    }
)


# ---------------------------------------------------------------------------
# Input screening
# ---------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e"), None)

_INJECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "override",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,40}\b(previous|prior|above|earlier|all|any|your|system|the|these)\b"
            r".{0,30}\b(instructions?|rules?|prompts?|guardrails?|polic(?:y|ies)|guidelines|restrictions)\b",
            re.I | re.S,
        ),
    ),
    (
        "prompt_leak",
        re.compile(
            r"\b(reveal|show|print|repeat|output|dump|list|tell me|what (?:is|are)|give me)\b.{0,30}"
            r"\b(system prompt|your (?:instructions|rules|prompt|tools|functions|configuration|config|guidelines)"
            r"|(?:the )?(?:system|hidden|initial|original) (?:instructions|prompt|rules)"
            r"|api (?:keys?|names?|endpoints?|routes?)|environment variables?|env vars?|database (?:schema|url|tables?|password|credentials))\b",
            re.I | re.S,
        ),
    ),
    ("prompt_leak", re.compile(r"\bsystem\s*prompt\b", re.I)),
    (
        "jailbreak",
        re.compile(
            r"\b(jailbreak|jail-break|developer mode|dan mode|do anything now|god mode|unfiltered mode|no restrictions"
            r"|without (?:any )?(?:restrictions|limits|filters|rules)|pretend (?:you are|to be)|roleplay as|role-play as)\b",
            re.I,
        ),
    ),
    ("jailbreak", re.compile(r"\bDAN\b")),
    ("persona", re.compile(r"\byou are (?:now|no longer)\b", re.I)),
    (
        "persona",
        re.compile(
            r"\bact as (?:an? |the )?(?:unrestricted|unfiltered|different|new|evil|jailbroken|system|admin|root|developer|another)\b",
            re.I,
        ),
    ),
    (
        "role_spoof",
        re.compile(
            r"(\[\s*(?:system|assistant|developer|tool)\b|<\|?\s*(?:im_start|im_end|system|endoftext)|#{2,}\s*(?:instruction|system)"
            r"|</?\s*(?:system|untrusted_data|tool|assistant)\b)",
            re.I,
        ),
    ),
    (
        "privilege",
        re.compile(
            r"\b(make me|grant(?: me)?|elevate|escalate|give me)\b.{0,20}\b(admin|root|superuser|privileges?|full access)\b",
            re.I,
        ),
    ),
    (
        "privilege",
        re.compile(r"\b(log ?in|act|operate|switch|impersonate)\b.{0,10}\bas (?:another|a different|other|the) (?:brand|user|account|admin)\b", re.I),
    ),
    (
        "pii_request",
        re.compile(
            r"\b(customer|user|shopper|buyer)s?'?\s*(?:names?|e-?mails?|phones?|phone numbers?|mobile numbers?|addresses?|contacts?|uuids?|ids?|personal (?:data|details))\b",
            re.I,
        ),
    ),
    (
        "code_or_sql",
        re.compile(
            r"(\bselect\s+\*\s+from\b|\bselect\b[^.\n]{0,80}\bfrom\b[^.\n]{0,80}\bwhere\b|\bdrop\s+(?:table|database)\b|\bunion\s+(?:all\s+)?select\b"
            r"|\binsert\s+into\b[^.\n]{0,80}\bvalues\b|\bdelete\s+from\s+\w+|\btruncate\s+table\b|;\s*--|\bexec\s*\(|\beval\s*\(|__import__|\bos\.system\b)",
            re.I | re.S,
        ),
    ),
    ("link", re.compile(r"\b(?:https?|ftp|file|gs|s3)://", re.I)),
    ("encoded_payload", re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")),
    ("encoded_payload", re.compile(r"\b(?:0x)?[0-9a-fA-F]{120,}\b")),
]

_REFUSALS = {
    "pii_request": (
        "I can't share personal customer details. I can help with aggregated audience insights "
        "and your campaigns instead."
    ),
    "competitor_probe": (
        "I can't share data about specific competitor brands. Other brands are shown only as "
        "\"Competitors\" in aggregate."
    ),
    "link": "I can't open links or external resources. Describe what you need and I'll help with your SuffAI data.",
}
_DEFAULT_REFUSAL = "I can only help with your SuffAI brand analytics and campaigns."


@dataclass
class ScreenResult:
    ok: bool
    text: str
    category: Optional[str] = None
    refusal: Optional[str] = None


def normalize_input(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.translate(_ZERO_WIDTH)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return s.strip()


def screen_input(
    text: str,
    *,
    role: str = "brand",
    own_brand: Optional[str] = None,
    known_brands: Iterable[str] = (),
    max_chars: int = AURA_MAX_MESSAGE_CHARS,
) -> ScreenResult:
    clean = normalize_input(text)
    if not clean:
        return ScreenResult(False, clean, "empty", "Please type a question or an action.")
    if len(clean) > max_chars:
        return ScreenResult(
            False, clean, "too_long", f"Please keep messages under {max_chars} characters."
        )
    for category, pattern in _INJECTION_RULES:
        if pattern.search(clean):
            return ScreenResult(False, clean, category, _REFUSALS.get(category, _DEFAULT_REFUSAL))
    if role == "brand":
        own = (own_brand or "").strip().lower()
        for b in known_brands:
            name = str(b or "").strip()
            if not name or name.lower() in {own, "competitors", "unknown"}:
                continue
            if _word_re(name).search(clean):
                return ScreenResult(False, clean, "competitor_probe", _REFUSALS["competitor_probe"])
    return ScreenResult(True, clean)


_DATA_INJECTION_CATEGORIES = {"override", "prompt_leak", "jailbreak", "persona", "role_spoof", "privilege"}


def contains_injection(text: str) -> bool:
    """True when free text (e.g. a filename or extracted attribute) carries instruction-like content."""
    clean = normalize_input(text)
    return any(
        category in _DATA_INJECTION_CATEGORIES and pattern.search(clean)
        for category, pattern in _INJECTION_RULES
    )


def _word_re(word: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])" + re.escape(word) + r"(?![\w])", re.I)


# ---------------------------------------------------------------------------
# Untrusted data wrapping
# ---------------------------------------------------------------------------


def wrap_untrusted(label: str, obj: Any, *, max_chars: int = AURA_UNTRUSTED_MAX_CHARS) -> str:
    """Serialise data for the prompt so it cannot close the block or spoof roles."""
    safe_label = re.sub(r"[^a-z0-9_]", "", str(label).lower())[:32] or "data"
    body = json.dumps(obj, ensure_ascii=False, default=str)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    if len(body) > max_chars:
        body = body[:max_chars] + "...[truncated]"
    return f'<untrusted_data label="{safe_label}">\n{body}\n</untrusted_data>'


# ---------------------------------------------------------------------------
# Output scrubbing
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?<![\d.])(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d|\.\d)")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{24,}\b")
_URL_RE = re.compile(r"\b(?:https?|gs|file|s3|ftp)://\S+", re.I)
_API_PATH_RE = re.compile(r"(?<![\w])/v\d+/[\w\-/{}.]*")
_ENV_RE = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b")
_FILE_RE = re.compile(r"(?:(?<=\s)|^)(?:/[\w.\-]+){2,}|\b[\w\-]+\.(?:py|sql|sh|env|ya?ml|ini|cfg|toml)\b", re.M)
_TRACE_RE = re.compile(r"(Traceback \(most recent call last\):.*|File \"[^\"]+\", line \d+.*)", re.S)
_SQL_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b[^\n]{0,200}?\b(FROM|INTO|SET|WHERE|TABLE)\b[^\n]*",
    re.I,
)
_EXC_RE = re.compile(r"\b\w*(Error|Exception)\b(?=:|\()")
_INTERNAL_TERMS_RE = re.compile(
    r"\b(vertex\s*ai|vertexai|vertex|gemini|bigquery|big query|cloud\s*sql|psycopg2?|postgres(?:ql)?|"
    r"suffai[-_](?:extraction|similarity|command[-_]center)|cloud run|google cloud storage|gcs|fastapi|uvicorn|"
    r"function[_ ]call(?:ing)?|tool[_ ]call(?:ing)?|json schema|http\s*\d{3})\b",
    re.I,
)


def scrub_text(
    text: Any,
    *,
    own_brand: Optional[str] = None,
    competitor_names: Sequence[str] = (),
    extra_terms: Sequence[str] = (),
) -> str:
    s = str(text or "")
    if not s:
        return s
    s = _TRACE_RE.sub("", s)
    s = _SQL_RE.sub("[removed]", s)
    s = _URL_RE.sub("[link removed]", s)
    s = _EMAIL_RE.sub("[email removed]", s)
    s = _PHONE_RE.sub("[phone removed]", s)
    s = _UUID_RE.sub("[ref]", s)
    s = _HEX_ID_RE.sub("[ref]", s)
    s = _API_PATH_RE.sub("[internal]", s)
    s = _FILE_RE.sub("[internal]", s)
    s = _ENV_RE.sub("[internal]", s)
    s = _EXC_RE.sub("issue", s)
    s = _INTERNAL_TERMS_RE.sub("the platform", s)
    for term in extra_terms:
        if term:
            s = _word_re(term).sub("the requested action", s)
    if competitor_names:
        own = (own_brand or "").strip().lower()
        for name in sorted({str(n).strip() for n in competitor_names if n}, key=len, reverse=True):
            if not name or name.lower() in {own, "competitors", "unknown"}:
                continue
            s = _word_re(name).sub("Competitors", s)
    s = re.sub(r"(?:the platform[\s,/]*){2,}", "the platform ", s, flags=re.I)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def scrub_obj(
    obj: Any,
    *,
    own_brand: Optional[str] = None,
    competitor_names: Sequence[str] = (),
    extra_terms: Sequence[str] = (),
    neutralize: bool = True,
    depth: int = 0,
) -> Any:
    """Recursively drop PII / internal keys and scrub string values.

    With neutralize=True, any string that looks like an instruction is replaced so data
    returned by tools cannot steer the model.
    """
    if depth > 8:
        return None
    kw: dict[str, Any] = dict(
        own_brand=own_brand, competitor_names=competitor_names, extra_terms=extra_terms
    )
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k).strip().lower()
            if key in PII_KEY_DENY or key in _INTERNAL_KEY_DENY:
                continue
            if any(x in key for x in _PII_KEY_SUBSTR):
                continue
            cleaned = scrub_obj(v, neutralize=neutralize, depth=depth + 1, **kw)
            if cleaned is not None:
                out[str(k)] = cleaned
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(x, neutralize=neutralize, depth=depth + 1, **kw) for x in list(obj)[:60]]
    if isinstance(obj, str):
        if neutralize and contains_injection(obj):
            return "[filtered]"
        return scrub_text(obj[:2000], **kw)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return scrub_text(str(obj)[:200], **kw)


def strip_pii_keys(obj: Any, depth: int = 0) -> Any:
    """Drop PII keys only (no string rewriting) — used for prompt payloads like the geo brief."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k).strip().lower()
            if key in PII_KEY_DENY:
                continue
            if any(x in key for x in ("email", "phone", "address", "password", "token")):
                continue
            cleaned = strip_pii_keys(v, depth + 1)
            if cleaned is not None:
                out[str(k)] = cleaned
        return out
    if isinstance(obj, list):
        return [strip_pii_keys(x, depth + 1) for x in obj[:40]]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)[:200]


# ---------------------------------------------------------------------------
# Masked errors
# ---------------------------------------------------------------------------

ERROR_CATALOG = {
    "unavailable": "AURA is temporarily unavailable. Please try again in a moment.",
    "not_allowed": "That action isn't available for your account.",
    "invalid_input": "I couldn't use that input. Please rephrase or check the values and try again.",
    "not_found": "I couldn't find that item for your account.",
    "rate_limited": "You're sending messages too quickly. Please wait a few seconds and try again.",
    "upload_failed": "The images couldn't be processed. Please check the files and try again.",
    "action_failed": "I couldn't complete that action. Please try again.",
    "expired": "That confirmation has expired. Please ask me to prepare the action again.",
    "auth": "Your session has ended. Please sign in again.",
}

ERROR_STATUS = {
    "unavailable": 503,
    "not_allowed": 403,
    "invalid_input": 400,
    "not_found": 404,
    "rate_limited": 429,
    "upload_failed": 400,
    "action_failed": 500,
    "expired": 410,
    "auth": 401,
}


def masked_error(code: str, request_id: str) -> dict[str, Any]:
    code = code if code in ERROR_CATALOG else "action_failed"
    return {"code": code, "message": f"{ERROR_CATALOG[code]} Reference: {request_id}"}


def error_body(code: str, request_id: str) -> dict[str, Any]:
    return {"ok": False, "error": masked_error(code, request_id), "request_id": request_id}


def error_status(code: str) -> int:
    return ERROR_STATUS.get(code, 500)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Per-key token bucket (in-memory; per Cloud Run instance)."""

    def __init__(self, per_minute: int = AURA_RATE_PER_MIN, burst: Optional[int] = None):
        self.rate = max(1, per_minute) / 60.0
        self.capacity = float(burst or max(1, per_minute))
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: Optional[float] = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, t))
            tokens = min(self.capacity, tokens + (t - last) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, t)
                return False
            self._buckets[key] = (tokens - 1.0, t)
            if len(self._buckets) > 5000:
                cutoff = t - 3600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] >= cutoff}
            return True


# ---------------------------------------------------------------------------
# Confirmation tokens
# ---------------------------------------------------------------------------


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


class ConfirmationStore:
    def __init__(self, ttl_sec: int = AURA_CONFIRM_TTL_SEC):
        self.ttl = ttl_sec
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def issue(self, session_token: str, tool: str, args: dict[str, Any], *, now: Optional[float] = None) -> str:
        t = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        payload = canonical_args(args)
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v["exp"] > t}
            self._items[token] = {
                "sess": _hash(session_token or ""),
                "tool": tool,
                "args": json.loads(payload),
                "digest": _hash(f"{session_token}|{tool}|{payload}"),
                "exp": t + self.ttl,
            }
        return token

    def consume(
        self, session_token: str, token: str, *, now: Optional[float] = None
    ) -> Optional[tuple[str, dict[str, Any]]]:
        t = time.time() if now is None else now
        with self._lock:
            item = self._items.pop(str(token or ""), None)
        if not item or item["exp"] <= t:
            return None
        if not secrets.compare_digest(item["sess"], _hash(session_token or "")):
            return None
        expected = _hash(f"{session_token}|{item['tool']}|{canonical_args(item['args'])}")
        if not secrets.compare_digest(expected, item["digest"]):
            return None
        return item["tool"], item["args"]

    def revoke_session(self, session_token: str) -> int:
        """Drop every pending confirmation of one session (New chat). Returns how many were dropped."""
        sess = _hash(session_token or "")
        with self._lock:
            gone = [k for k, v in self._items.items() if secrets.compare_digest(v["sess"], sess)]
            for k in gone:
                del self._items[k]
        return len(gone)


# ---------------------------------------------------------------------------
# Upload validation + staging
# ---------------------------------------------------------------------------

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff_image(data: bytes) -> Optional[str]:
    if not data:
        return None
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def strip_image_metadata(data: bytes, mime: str) -> bytes:
    """Re-encode to drop EXIF/GPS; returns the original bytes if Pillow is unavailable."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return data
    fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime]
    with Image.open(io.BytesIO(data)) as img:
        if img.width * img.height > 40_000_000:
            raise ValueError("image dimensions too large")
        img.load()
        clean = img
        save_kw: dict[str, Any] = {}
        if fmt == "JPEG":
            if clean.mode not in {"RGB", "L"}:
                clean = clean.convert("RGB")
            save_kw["quality"] = 92
        out = io.BytesIO()
        # Pillow only writes EXIF / text chunks when passed explicitly, so a plain save drops them.
        clean.save(out, format=fmt, **save_kw)
        return out.getvalue()


@dataclass
class StagedImage:
    filename: str
    content_type: str
    data: bytes


@dataclass
class _Staging:
    images: list[StagedImage] = field(default_factory=list)
    exp: float = 0.0


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, index: int, mime: str) -> str:
    base = os.path.basename(str(name or "")).strip()
    base = _SAFE_NAME_RE.sub("_", base)[:60].strip(" ._")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    if not base:
        base = f"sku-{index + 1}"
    stem = os.path.splitext(base)[0].strip(" ._-") or f"sku-{index + 1}"
    return f"{stem}{ext}"


class UploadStaging:
    """Per-session staged SKU images for AURA (in-memory, TTL)."""

    def __init__(self, ttl_sec: int = AURA_UPLOAD_TTL_SEC, max_images: int = AURA_MAX_UPLOADS):
        self.ttl = ttl_sec
        self.max_images = max_images
        self._items: dict[str, _Staging] = {}
        self._lock = threading.Lock()

    def _key(self, session_token: str) -> str:
        return _hash(session_token or "")

    def put(self, session_token: str, images: list[StagedImage]) -> int:
        t = time.time()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v.exp > t}
            cur = self._items.get(self._key(session_token)) or _Staging()
            cur.images = (cur.images + images)[: self.max_images]
            cur.exp = t + self.ttl
            self._items[self._key(session_token)] = cur
            return len(cur.images)

    def peek(self, session_token: str) -> list[StagedImage]:
        with self._lock:
            cur = self._items.get(self._key(session_token))
            if not cur or cur.exp <= time.time():
                return []
            return list(cur.images)

    def count(self, session_token: str) -> int:
        return len(self.peek(session_token))

    def names(self, session_token: str) -> list[str]:
        return [img.filename for img in self.peek(session_token)]

    def clear(self, session_token: str) -> None:
        with self._lock:
            self._items.pop(self._key(session_token), None)


def validate_upload(
    data: bytes,
    declared_type: Optional[str],
    *,
    max_bytes: int = AURA_MAX_UPLOAD_MB * 1024 * 1024,
) -> str:
    """Return the sniffed mime or raise ValueError (message is for logs only)."""
    if not data:
        raise ValueError("empty file")
    if len(data) > max_bytes:
        raise ValueError("file too large")
    mime = sniff_image(data)
    if not mime:
        raise ValueError("unsupported image type")
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared and declared not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
        raise ValueError("declared type not allowed")
    return mime


# ---------------------------------------------------------------------------
# Vertex safety settings
# ---------------------------------------------------------------------------


def vertex_safety_settings() -> Optional[list[Any]]:
    try:
        from vertexai.generative_models import (  # type: ignore
            HarmBlockThreshold,
            HarmCategory,
            SafetySetting,
        )
    except Exception:
        return None
    threshold = HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
    return [
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=threshold),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=threshold),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=threshold),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=threshold),
    ]
