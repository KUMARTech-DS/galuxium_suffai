from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import get_garment_store, get_normalized, get_rfm
from suffai.api.chat_tools import build_tool_context, route_chat_query
from suffai.api.envelope import ok

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatRequest(BaseModel):
    query: str
    filter_context: Optional[Dict[str, Any]] = None


@router.post("/query")
def chat_query(body: ChatRequest):
    norm = get_normalized()
    ctx = build_tool_context(
        norm["orders"],
        norm["order_commodity_lines"],
        get_rfm(),
        get_garment_store(),
    )
    result = route_chat_query(body.query, ctx)
    return ok(
        result,
        coverage={
            "tool": result.get("tool"),
            "refused": result.get("refused"),
            "filters": result.get("filters"),
        },
    )
