from pathlib import Path

from suffai.api.chat_tools import build_tool_context, route_chat_query
from suffai.garments.store import GarmentStore
from suffai.ingest import load_source_collections, normalize_all
from suffai.metrics.rfm import compute_rfm_snapshots

DATA = Path(__file__).resolve().parents[1] / "data_offline"


def _ctx(tmp_path):
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    return build_tool_context(
        norm["orders"],
        norm["order_commodity_lines"],
        compute_rfm_snapshots(norm["orders"], norm["stores"]),
        GarmentStore(tmp_path),
    )


def test_chat_refuses_market_share(tmp_path):
    out = route_chat_query("What is our fashion market share?", _ctx(tmp_path))
    assert out["refused"] is True


def test_chat_commodity_pin(tmp_path):
    out = route_chat_query(
        "Which garment types are most popular in PIN 560103?",
        _ctx(tmp_path),
    )
    assert out["tool"] == "commodity_popularity"
    assert out["result"]["commodities"]
