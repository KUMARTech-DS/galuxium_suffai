"""Vertex multimodal embeddings — same init / GCS-URI-first pattern as suffai-extraction."""

from __future__ import annotations

import hashlib
import logging
import math
import struct
from typing import List, TYPE_CHECKING

from similarity.settings import settings

if TYPE_CHECKING:
    from similarity.gcs_io import GcsImage

log = logging.getLogger("similarity.embed")


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


def _stub_embedding(image_bytes: bytes, dim: int) -> List[float]:
    """Deterministic pseudo-embedding for local/dev without Vertex."""
    seed = hashlib.sha256(image_bytes).digest()
    vals: List[float] = []
    counter = 0
    while len(vals) < dim:
        block = hashlib.sha256(seed + struct.pack(">I", counter)).digest()
        for i in range(0, len(block) - 3, 4):
            raw = struct.unpack(">I", block[i : i + 4])[0]
            vals.append((raw / 0xFFFFFFFF) * 2.0 - 1.0)
            if len(vals) >= dim:
                break
        counter += 1
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]


def embed_image(image: "GcsImage") -> tuple[List[float], str]:
    """
    Returns (embedding, model_name).

    Mirrors worker/vertex_extract.py:
      - vertexai.init(project, location)
      - Prefer GCS URI (no re-upload); fall back to inline bytes
      - Gated by ENABLE_VERTEX
    """
    dim = settings.EMBED_DIMENSION
    model_name = settings.VERTEX_EMBED_MODEL

    if not settings.ENABLE_VERTEX:
        log.warning("ENABLE_VERTEX=false; using stub embedding dim=%s", dim)
        return _stub_embedding(image.data, dim), f"stub-{model_name}"

    if not settings.PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT / GCP_PROJECT is required for Vertex")

    import vertexai
    from vertexai.vision_models import Image, MultiModalEmbeddingModel

    vertexai.init(project=settings.PROJECT_ID, location=settings.VERTEX_LOCATION)
    model = MultiModalEmbeddingModel.from_pretrained(model_name)

    try:
        # Prefer GCS object (same idea as Part.from_uri in extraction)
        try:
            img = Image.load_from_file(image.gs_uri)
        except Exception as uri_exc:
            log.warning("Vertex GCS URI embed failed (%s); retrying with inline bytes", uri_exc)
            img = Image(image_bytes=image.data)
        result = model.get_embeddings(image=img, dimension=dim)
        emb = list(result.image_embedding or [])
        if not emb:
            raise RuntimeError("Vertex returned empty image embedding")
        log.info(
            "vertex_embed_ok model=%s dim=%s gs_uri=%s",
            model_name,
            len(emb),
            image.gs_uri,
        )
        return emb, model_name
    except Exception:
        log.exception("Vertex embedding failed gs_uri=%s", image.gs_uri)
        raise
