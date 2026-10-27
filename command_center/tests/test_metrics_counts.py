"""One garment count: photographed garments per current customer PIN, as compare-pins sees them."""

import json

import admin_intel as ai
import aggregate_metrics as agg
import campaign_service as svc
import pin_garments
import pytest

CUSTOMERS = [
    {"_id": "c1", "pin_code": "560034", "age": 30, "gender": "Female", "rfm_segment": "Loyal"},
    {"_id": "c2", "pin_code": "560001", "age": 41, "gender": "Male", "rfm_segment": "New"},
]


def _item(cid, ugid, key="garments/x.jpg", **over):
    return {"customer_id": cid, "unique_garment_id": ugid, "gcs_object_key": key, "brand": "Zara", **over}


ITEMS = [
    _item("c1", "g1"),
    _item("c1", "g2"),
    _item("c2", "g1"),                       # the same garment seen in another PIN counts there too
    _item("c1", None),                       # photo not matched by similarity yet
    _item("c1", "g7", key=None),             # CSV row: never compared
    _item("gone", "g5"),                     # customer no longer exists
]


def test_photo_garments_per_current_pin():
    geo = agg.build(CUSTOMERS, [], ITEMS, {})["pin_geo"]
    assert (geo["560034"]["photo_garments"], geo["560001"]["photo_garments"]) == (2, 1)


def test_photo_garments_capped_like_compare_pins(monkeypatch):
    monkeypatch.setenv("COMPARE_PIN_CANDIDATE_LIMIT", "1")
    assert agg.build(CUSTOMERS, [], ITEMS, {})["pin_geo"]["560034"]["photo_garments"] == 1


REPEAT = _item("c1", "g2", key="garments/y.jpg", is_repeat=True)  # second photo of c1's g2


@pytest.fixture
def admin_data(monkeypatch):
    monkeypatch.setattr(ai, "_dataset_loader", lambda: (CUSTOMERS, [], ITEMS + [REPEAT]))
    ai.clear_cache()
    yield
    ai.clear_cache()


def test_tile_admin_and_ranking_corpus_are_one_count(admin_data):
    """Metrics leave repeat photos out, Admin keeps them: the garments per PIN are the same."""
    geo = agg.build(CUSTOMERS, [], ITEMS, {})["pin_geo"]
    admin = {r["pin_code"]: r for r in ai.unique_garments()["by_pin"]}
    corpus = pin_garments.corpus(ai.dataset(), ["560034", "560001", "110001"])
    for pin in ("560034", "560001"):
        assert geo[pin]["photo_garments"] == admin[pin]["unique_garments"] == len(corpus["candidates"][pin])
        assert corpus["pin_totals"][pin] == admin[pin]["observations"]
    assert corpus["candidates"]["560034"] == [["g2", 2], ["g1", 1]]  # most photographed first
    assert corpus["candidates"]["110001"] == [] and corpus["pin_totals"]["110001"] == 0


def test_corpus_is_capped_like_the_tile(admin_data, monkeypatch):
    monkeypatch.setenv("COMPARE_PIN_CANDIDATE_LIMIT", "1")
    corpus = pin_garments.corpus(ai.dataset(), ["560034"])
    assert corpus["candidates"]["560034"] == [["g2", 2]] and corpus["pin_totals"]["560034"] == 3


def test_compare_pins_request_sends_the_corpus(admin_data, monkeypatch):
    import urllib.request

    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"aggregate_fit": 0.5, "per_pin": {}}'

    def fake_urlopen(req, timeout=None):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    corpus = svc.garment_corpus(["560034"])
    svc._call_compare_pins(similarity_url="https://sim", bucket="b", object_key="brands/z/c/s.jpg",
                           pin_codes=["560034"], token=None, corpus=corpus)
    assert sent["url"] == "https://sim/v1/similarity/compare-pins"
    assert sent["body"]["candidates"] == {"560034": [["g2", 2], ["g1", 1]]}
    assert sent["body"]["pin_totals"] == {"560034": 3}


def test_garment_corpus_unavailable_is_a_service_error(monkeypatch):
    def boom():
        raise RuntimeError("bq down")

    monkeypatch.setattr(ai, "_dataset_loader", boom)
    ai.clear_cache()
    try:
        with pytest.raises(svc.ServiceError) as exc:
            svc.garment_corpus(["560034"])
        assert exc.value.status == 503
    finally:
        ai.clear_cache()
