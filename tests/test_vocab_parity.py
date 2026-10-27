"""One value key everywhere: worker/vocab.py, command_center/vocab.py and the SQL migration agree."""

import importlib.util
import re
from pathlib import Path

import pytest

from worker import bq_items, vocab as wvocab
from worker.photo_key import PhotoKey

ROOT = Path(__file__).resolve().parents[1]


def _cc_vocab():
    spec = importlib.util.spec_from_file_location("cc_vocab", ROOT / "command_center" / "vocab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CC = _cc_vocab()
SAMPLES = ["Levi's", "LEVIS", " levis ", "H&M", "H & M", "T-Shirt", "tshirt", "Navy_Blue", "navy blue", "Café",
           "Not Applicable", "N/A", "", None, 12, "Allen Solly", "U.S. Polo Assn."]


@pytest.mark.parametrize("value", SAMPLES)
def test_both_copies_give_the_same_key(value):
    assert wvocab.value_key(value) == CC.value_key(value)


def test_keys_group_spellings():
    k = wvocab.value_key
    assert k("Levi's") == k("LEVIS") == k(" levis ") == "levis"
    assert k("H&M") == k("H & M") == "hm"
    assert k("T-Shirt") == k("tshirt") and k("Navy_Blue") == k("navy blue") == "navyblue"
    assert wvocab.is_unknown("N/A") and wvocab.is_unknown("Not Applicable") and wvocab.is_unknown(None)


def test_copies_share_constants_and_sql_uses_the_same_rule():
    assert wvocab.DEFAULTS == CC.DEFAULTS and wvocab.UNKNOWN_KEYS == CC.UNKNOWN_KEYS and wvocab.KINDS == CC.KINDS
    assert wvocab._KEY_RE.pattern == CC._KEY_RE.pattern == "[^a-z0-9]"
    sql = (ROOT / "command_center" / "scripts" / "migrate_bq_consistency.sql").read_text(encoding="utf-8")
    assert "REGEXP_REPLACE(LOWER(TRIM(c.brand)), r'[^a-z0-9]', '')" in sql
    unknown = re.search(r"alias_key NOT IN \(([^)]*)\)", sql).group(1)
    assert {s.strip().strip("'") for s in unknown.split(",")} == set(wvocab.UNKNOWN_KEYS)
    seeded = set(re.findall(r"\('(customer_gender|garment_gender)', '(\w+)', '(\w+)'\)", sql))
    seeded |= {("customer_gender", "Female", "female")}  # the first row is written as a STRUCT
    for kind, mapping in wvocab.DEFAULTS.items():
        assert {(kind, canon, key) for key, canon in mapping.items()} <= seeded


REF_ROWS = [
    {"kind": "brand", "canonical": "Levi's", "alias_key": "levis"},
    {"kind": "color", "canonical": "Navy Blue", "alias_key": "navy"},
    {"kind": "color", "canonical": "Navy Blue", "alias_key": "navyblue"},
    {"kind": "garment_name", "canonical": "T-Shirt", "alias_key": "tshirt"},
]


@pytest.fixture
def ref(monkeypatch):
    monkeypatch.setattr(wvocab, "_loader", lambda: REF_ROWS)
    wvocab.clear_cache()
    yield
    wvocab.clear_cache()


def _row(**result):
    base = {"commodity_type": "tshirt", "display_name": "T Shirt", "primary_color": "navy", "brand_name": "LEVIS",
            "garment_department": "Men", "model_version": "gemini-test"}
    return bq_items.item_row(PhotoKey("c1", "o1", "1"), object_key="k", result={**base, **result}, status="COMPLETED")


def test_extraction_writes_canonical_values(ref):
    row = _row()
    assert (row["brand"], row["color"], row["name"], row["gender"]) == ("Levi's", "Navy Blue", "T-Shirt", "Male")


@pytest.mark.parametrize("over, field, expected", [
    ({"brand_name": "unknown"}, "brand", "unknown"),
    ({"brand_name": "Zudio"}, "brand", "Zudio"),            # not in ref_values yet: kept as read
    ({"garment_department": "other"}, "gender", "Unisex"),
    ({"garment_department": "maternity"}, "gender", "unknown"),
    ({"primary_color": "not_applicable"}, "color", "unknown"),
])
def test_extraction_unknown_and_new_values(ref, over, field, expected):
    assert _row(**over)[field] == expected


def test_unreadable_ref_values_fall_back_to_fixed_lists(monkeypatch):
    calls = []

    def down():
        calls.append(1)
        raise RuntimeError("bq down")

    monkeypatch.setattr(wvocab, "_loader", down)
    wvocab.clear_cache()
    assert wvocab.canonical("garment_gender", "womens") == "Female"
    assert wvocab.canonical("brand", "Levis") == "Levis"
    wvocab.canonical("brand", "x")
    assert len(calls) == 1  # failed reads are retried after RETRY_SEC, not on every row
    wvocab.clear_cache()


def test_prompt_lists_preferred_spellings(ref):
    vertex = pytest.importorskip("worker.vertex_extract")
    hint = vertex.preferred_values_hint()
    assert "Levi's" in hint and "Navy Blue" in hint and "T-Shirt" in hint
