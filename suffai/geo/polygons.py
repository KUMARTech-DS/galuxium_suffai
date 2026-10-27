"""PIN polygon FeatureCollection for choropleth maps."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_GEOJSON = Path(__file__).resolve().parent / "pin_polygons.geojson"


@lru_cache(maxsize=1)
def load_pin_polygons() -> dict[str, Any]:
    return json.loads(_GEOJSON.read_text(encoding="utf-8"))


def choropleth_features(tiles: list[dict[str, Any]], *, metric_key: str = "active_customers") -> dict[str, Any]:
    """Join aggregate tiles onto PIN polygons; suppressed cells get null metric."""
    by_pin = {str(t.get("pin_code") or t.get("geo_key")): t for t in tiles}
    base = load_pin_polygons()
    features = []
    for f in base.get("features") or []:
        props = dict(f.get("properties") or {})
        pin = str(props.get("pin_code"))
        tile = by_pin.get(pin) or {}
        suppressed = bool(tile.get("suppressed"))
        value = None if suppressed else tile.get("active_customers")
        props.update({
            "metric_key": metric_key,
            "metric_value": value,
            "suppressed": suppressed,
            "denominator": tile.get("denominator"),
        })
        features.append({"type": "Feature", "properties": props, "geometry": f.get("geometry")})
    return {
        "type": "FeatureCollection",
        "disclaimer": base.get("disclaimer"),
        "features": features,
    }
