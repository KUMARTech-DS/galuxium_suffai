"""Optional Vertex shadow scorer for 1P fee models.

The cost model remains the serving authority until enough real accepted/final-fee
labels exist. This module only records a Vertex prediction for comparison.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("command_center.campaign_fee_model")


def shadow_prediction(features: dict[str, Any]) -> Optional[float]:
    """Return a Vertex prediction when explicitly enabled; never raise to callers."""
    endpoint_id = (os.getenv("VERTEX_FEE_ENDPOINT_ID") or "").strip()
    if not endpoint_id or os.getenv("FEE_VERTEX_SHADOW", "").lower() not in {"1", "true", "yes"}:
        return None
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("BQ_PROJECT") or "").strip()
    location = (os.getenv("VERTEX_LOCATION") or "asia-south1").strip()
    if not project:
        return None
    try:
        from google.cloud import aiplatform_v1  # type: ignore
        from google.protobuf import struct_pb2  # type: ignore

        endpoint = endpoint_id if endpoint_id.startswith("projects/") else (
            f"projects/{project}/locations/{location}/endpoints/{endpoint_id}"
        )
        instance = struct_pb2.Value()
        instance.struct_value.update({
            "audience_count": features.get("audience_count", 0),
            "sku_count": features.get("sku_count", 0),
            "pin_count": features.get("pin_count", 0),
            "comparisons": features.get("comparisons", 0),
            "process_seconds": features.get("process_seconds", 0.0),
            "duration_days": features.get("duration_days", 1),
            "media_budget_inr": features.get("media_budget_inr", 0.0),
            "channel_count": len(features.get("channels") or []),
            "objective": features.get("objective") or "consideration",
        })
        client = aiplatform_v1.PredictionServiceClient(
            client_options={"api_endpoint": f"{location}-aiplatform.googleapis.com"}
        )
        response = client.predict(endpoint=endpoint, instances=[instance])
        if not response.predictions:
            return None
        prediction = response.predictions[0]
        if prediction.HasField("number_value"):
            return float(prediction.number_value)
        if prediction.HasField("struct_value"):
            for key in ("prediction", "fee_inr", "value"):
                value = prediction.struct_value.fields.get(key)
                if value and value.HasField("number_value"):
                    return float(value.number_value)
    except Exception:
        log.warning("Vertex fee shadow prediction unavailable", exc_info=True)
    return None
