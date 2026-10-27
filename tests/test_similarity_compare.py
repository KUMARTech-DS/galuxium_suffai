"""compare-pins scoring against the PIN garments Command Center sends (no GCP / DB)."""

import sys
import types

import pytest

if "google.cloud.storage" not in sys.modules:
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        google = sys.modules.setdefault("google", types.ModuleType("google"))
        cloud = types.ModuleType("google.cloud")
        cloud.storage = types.ModuleType("google.cloud.storage")
        google.cloud = cloud
        sys.modules["google.cloud"] = cloud
        sys.modules["google.cloud.storage"] = cloud.storage

from similarity import compare  # noqa: E402

MODEL = "multimodalembedding@001"


def _g(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


RED, BLUE, OPPOSITE, MIXED, STUB, SHORT, GONE = (_g(i) for i in range(1, 8))
EMBEDDINGS = {
    RED: ([1.0, 0.0, 0.0], MODEL),
    BLUE: ([0.0, 1.0, 0.0], MODEL),
    OPPOSITE: ([-1.0, 0.0, 0.0], MODEL),
    MIXED: ([1.0, 1.0, 0.0], MODEL),
    STUB: ([1.0, 0.0, 0.0], "stub-" + MODEL),
    SHORT: ([1.0, 0.0], MODEL),
}
CANDIDATES = {
    "560008": [[RED, 10], [BLUE, 2], [OPPOSITE, 50]],
    "110001": [[MIXED, 4], [STUB, 9], [SHORT, 9], [GONE, 3]],
}
TOTALS = {"560008": 62, "110001": 22}


def _loader(ids):
    return {g: EMBEDDINGS[g] for g in ids if g in EMBEDDINGS}


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(compare.settings, "COMPARE_CACHE_SEC", 0)
    compare._cache.clear()


def _cands(pins, candidates=CANDIDATES, totals=TOTALS):
    corpus, tot = compare.normalize_corpus(pins, {p: candidates[p] for p in pins if p in candidates}, totals)
    cands, missing = compare.load_candidates(pins, corpus, MODEL, 3, loader=_loader)
    return cands, tot, missing


def test_normalize_pins():
    assert compare.normalize_pins(["560008", " 560008 ", "110001", ""]) == ["560008", "110001"]
    with pytest.raises(ValueError):
        compare.normalize_pins([])
    with pytest.raises(ValueError):
        compare.normalize_pins(["5600"])


def test_normalize_corpus_validates_the_request(monkeypatch):
    corpus, totals = compare.normalize_corpus(["560008", "400001"], {"560008": [[RED.upper(), "3"]]}, None)
    assert corpus == {"560008": [(RED, 3)], "400001": []}
    assert totals == {"560008": 3, "400001": 0}
    with pytest.raises(ValueError, match="candidates required"):
        compare.normalize_corpus(["560008"], None)
    with pytest.raises(ValueError, match="not in pin_codes"):
        compare.normalize_corpus(["560008"], {"110001": []})
    with pytest.raises(ValueError, match="UUID"):
        compare.normalize_corpus(["560008"], {"560008": [["g-red", 1]]})
    monkeypatch.setattr(compare.settings, "COMPARE_MAX_CANDIDATES", 1)
    with pytest.raises(ValueError, match="at most 1"):
        compare.normalize_corpus(["560008"], {"560008": [[RED, 1], [BLUE, 1]]})


def test_candidates_skip_other_models_sizes_and_missing_embeddings():
    cands, totals, missing = _cands(["560008", "110001"])
    assert {c[1] for c in cands} == {RED, BLUE, OPPOSITE, MIXED}
    assert totals == TOTALS
    assert missing == 1  # GONE: in BigQuery, no Cloud SQL embedding


def test_score_pins_shape_and_values():
    pins = ["560008", "110001", "400001"]
    cands, totals, _ = _cands(pins)
    out = compare.score_pins([2.0, 0.0, 0.0], pins, cands, totals, topk=2)
    p = out["per_pin"]["560008"]
    assert [m["garment_id"] for m in p["top_matches"]] == [RED, BLUE]
    assert p["top_matches"][0] == {"garment_id": RED, "similarity": 1.0, "observation_count": 10}
    assert p["pin_fit"] == pytest.approx(10 / 12, abs=1e-4)
    assert p["total_observations"] == 62
    assert out["per_pin"]["110001"]["pin_fit"] == pytest.approx(0.7071, abs=1e-4)
    empty = out["per_pin"]["400001"]
    assert empty == {"pin_fit": 0.0, "total_observations": 0, "candidates": 0, "top_matches": []}
    expected = (p["pin_fit"] * 62 + 0.7071 * 22) / 84
    assert out["aggregate_fit"] == pytest.approx(expected, abs=1e-3)
    assert 0.0 <= out["aggregate_fit"] <= 1.0


def test_negative_similarity_floors_at_zero():
    cands, totals, _ = _cands(["560008"])
    out = compare.score_pins([-1.0, 0.0, 0.0], ["560008"], cands, totals, topk=1)
    assert out["per_pin"]["560008"]["top_matches"][0]["garment_id"] == OPPOSITE
    out = compare.score_pins([0.0, 0.0, 1.0], ["560008"], cands, totals, topk=3)
    assert out["aggregate_fit"] == 0.0


def test_no_garments_anywhere():
    out = compare.score_pins([1.0, 0.0, 0.0], ["400001"], [], {}, topk=5)
    assert out["aggregate_fit"] == 0.0


def test_matches_command_center_contract():
    """Keys read by command_center brand_campaign.freq_coverage_from_compare / upsert_pin_scores."""
    pins = ["560008"]
    cands, totals, _ = _cands(pins)
    detail = compare.score_pins([1.0, 0.0, 0.0], pins, cands, totals, topk=5)["per_pin"]["560008"]
    assert {"pin_fit", "top_matches", "total_observations"} <= set(detail)
    assert all({"observation_count"} <= set(m) for m in detail["top_matches"])
    assert all("brand_name" not in m for m in detail["top_matches"])


def test_embedding_cache_loads_each_garment_once(monkeypatch):
    monkeypatch.setattr(compare.settings, "COMPARE_CACHE_SEC", 60)
    calls = []

    def loader(ids):
        calls.append(sorted(ids))
        return _loader(ids)

    corpus, _ = compare.normalize_corpus(["560008"], {"560008": CANDIDATES["560008"]})
    compare.load_candidates(["560008"], corpus, MODEL, 3, loader=loader)
    corpus2, _ = compare.normalize_corpus(["560008"], {"560008": [[RED, 1], [MIXED, 1]]})
    compare.load_candidates(["560008"], corpus2, MODEL, 3, loader=loader)
    assert calls == [sorted([RED, BLUE, OPPOSITE]), [MIXED]]


def test_compare_pins_uses_the_sent_candidates(monkeypatch):
    monkeypatch.setattr(compare.settings, "DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(compare, "read_image", lambda key, bucket=None: types.SimpleNamespace(object_key=key))
    monkeypatch.setattr(compare, "embed_image", lambda image: ([1.0, 0.0, 0.0], MODEL))
    monkeypatch.setattr(compare.db, "load_embeddings", _loader)
    out = compare.compare_pins(gcs_object_key="brands/z/c/s.jpg", gcs_bucket="b", pin_codes=["110001"],
                               candidates={"110001": CANDIDATES["110001"]}, pin_totals={"110001": 22})
    assert out["missing_embeddings"] == 1
    assert [m["garment_id"] for m in out["per_pin"]["110001"]["top_matches"]] == [MIXED]
    with pytest.raises(ValueError):
        compare.compare_pins(gcs_object_key="k", gcs_bucket="b", pin_codes=["110001"], candidates=None)
