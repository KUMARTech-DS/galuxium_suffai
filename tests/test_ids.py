from suffai.ids import extract_id, extract_datetime, normalize_pin


def test_extract_oid():
    assert extract_id({"$oid": "abc123"}) == "abc123"
    assert extract_id("abc123") == "abc123"
    assert extract_id(None) is None


def test_extract_date_number_long():
    dt = extract_datetime({"$date": {"$numberLong": "1700000000000"}})
    assert dt is not None
    assert dt.year == 2023


def test_normalize_pin_no_invention():
    assert normalize_pin("HSR 560102") == "560102"
    assert normalize_pin(None) is None
    assert normalize_pin("") is None
