"""GST arithmetic for POS documents."""

from datetime import date

import pytest

import retail_gst as gst


def line(mrp, qty=1, disc=0, rule="apparel", fixed=None, inter=False):
    return gst.price_line(mrp_paise=mrp, qty=qty, discount_bp=disc, rule=rule, fixed_bp=fixed, inter_state=inter)


@pytest.mark.parametrize("mrp,rate", [(262500, 500), (262501, 1800), (99900, 500), (499900, 1800)])
def test_apparel_slab_uses_taxable_value_per_piece(mrp, rate):
    assert line(mrp)["gst_rate_bp"] == rate


def test_slab_is_per_piece_after_discount():
    assert line(300000, qty=3)["gst_rate_bp"] == 1800
    assert line(300000, qty=3, disc=1500)["gst_rate_bp"] == 500


def test_line_total_equals_price_paid_and_tax_splits():
    ln = line(199900, qty=2, disc=1000)
    assert ln["total_paise"] == ln["gross_paise"] - ln["discount_paise"] == 359820
    assert ln["cgst_paise"] == ln["sgst_paise"] and ln["igst_paise"] == 0
    inter = line(199900, qty=2, disc=1000, inter=True)
    assert inter["igst_paise"] == ln["cgst_paise"] + ln["sgst_paise"] and inter["cgst_paise"] == 0


@pytest.mark.parametrize("inter", [False, True])
def test_every_line_adds_back_to_its_price(inter):
    for mrp in (99, 49900, 199900, 262500, 262501, 499900, 1299999):
        for qty in (1, 3):
            for disc in (0, 333, 1000, 5000):
                ln = line(mrp, qty=qty, disc=disc, inter=inter)
                value = ln["gross_paise"] - ln["discount_paise"]
                assert ln["taxable_paise"] + ln["cgst_paise"] + ln["sgst_paise"] + ln["igst_paise"] == ln["total_paise"] == value
                assert ln["cgst_paise"] == ln["sgst_paise"]
                assert abs(ln["taxable_paise"] * (10000 + ln["gst_rate_bp"]) - value * 10000) <= 10000 + ln["gst_rate_bp"]


def test_fixed_rate_rule():
    assert line(100000, rule="fixed", fixed=1200)["gst_rate_bp"] == 1200
    assert line(100000, rule="fixed", fixed=0)["taxable_paise"] == 100000


def test_bill_rounds_to_rupee():
    t = gst.bill_totals([line(99950), line(50025)])
    assert t["total_paise"] == 150000 and t["round_off_paise"] == 25 and t["units"] == 2


@pytest.mark.parametrize("mrp,qty,disc,inter,parts", [
    (99900, 3, 0, False, (1, 1, 1)), (199900, 2, 0, False, (1, 1)), (129900, 7, 333, True, (2, 1, 4)),
    (499900, 5, 1250, False, (3, 2)),
])
def test_partial_credits_add_up_to_the_line(mrp, qty, disc, inter, parts):
    orig = line(mrp, qty=qty, disc=disc, inter=inter)
    done = {k: 0 for k in gst.AMOUNT_KEYS}
    returned = 0
    for q in parts:
        c = gst.credit_line(orig, qty=q, returned_before=returned, credited_before=done)
        returned += q
        assert c["taxable_paise"] + c["cgst_paise"] + c["sgst_paise"] + c["igst_paise"] == c["total_paise"]
        assert c["total_paise"] == c["gross_paise"] - c["discount_paise"] and c["cgst_paise"] == c["sgst_paise"]
        for k in gst.AMOUNT_KEYS:
            done[k] += c[k]
    assert all(done[k] == orig[k] for k in gst.AMOUNT_KEYS)
    with pytest.raises(ValueError):
        gst.credit_line(orig, qty=1, returned_before=qty, credited_before=done)


def test_gstin_checksum():
    good = "29ABCDE1234F1Z" + gst.gstin_check_char("29ABCDE1234F1Z")
    assert gst.normalize_gstin(good.lower()) == good
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    with pytest.raises(ValueError):
        gst.normalize_gstin(bad)
    with pytest.raises(ValueError):
        gst.normalize_gstin("99ABCDE1234F1Z5")
    assert gst.normalize_gstin("  ") is None


@pytest.mark.parametrize("d,fy", [(date(2026, 4, 1), "2627"), (date(2027, 3, 31), "2627"), (date(2026, 3, 31), "2526")])
def test_financial_year(d, fy):
    assert gst.financial_year(d) == fy


def test_document_numbers_fit_gst_limit():
    assert gst.invoice_number("BLR01", "2627", 123) == "BLR012627-000123"
    assert gst.invoice_number("MUM12", "2627", 4, credit_note=True) == "MUM122627R000004"
    assert len(gst.invoice_number("ABCDE", "2627", 999999)) == 16


@pytest.mark.parametrize("paise,words", [
    (0, "Rupees Zero Only"),
    (105050, "Rupees One Thousand Fifty and Fifty Paise Only"),
    (1234567800, "Rupees One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight Only"),
])
def test_amount_in_words(paise, words):
    assert gst.amount_in_words(paise) == words


@pytest.mark.parametrize("v,p", [("1299", 129900), ("1,299.5", 129950), (12.05, 1205), ("0.01", 1)])
def test_to_paise(v, p):
    assert gst.to_paise(v) == p


@pytest.mark.parametrize("v", ["12.345", "abc", None, ""])
def test_to_paise_rejects(v):
    with pytest.raises(ValueError):
        gst.to_paise(v)
