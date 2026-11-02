"""Charge ↔ estorno cancellation pairing (pure logic, no DB)."""

from src.services.statement_cancellations import (
    find_cancellations,
    locked_from_matches,
    partner_info,
)


def _s(sid, rows):
    return {"id": sid, "filename": f"fatura_{sid}.pdf", "transactions": rows}


def _t(date, desc, amount, credit=None):
    row = {"date": date, "description": desc, "amount": amount}
    if credit is not None:
        row["raw_data"] = {"credit": credit}
    return row


def test_exact_pair_same_statement():
    st = _s(
        1,
        [
            _t("2026-09-08", "EBN*BOOKING COM", 1920.60),
            _t("2026-09-08", "EBN*BOOKING COM", -1920.60, True),
        ],
    )
    out = find_cancellations([st])
    assert out == {(1, 0): (1, 1), (1, 1): (1, 0)}


def test_description_and_amount_must_match():
    st = _s(
        1,
        [
            _t("2026-09-08", "EBN*BOOKING COM", 1920.60),
            _t("2026-09-09", "AIRBNB", -1920.60),
            _t("2026-09-09", "EBN*BOOKING COM", -1920.61),
        ],
    )
    assert find_cancellations([st]) == {}


def test_case_and_whitespace_insensitive():
    st = _s(
        1, [_t("2026-09-08", "Ebn*Booking  Com ", 10.0), _t("2026-09-10", "EBN*BOOKING COM", -10.0)]
    )
    assert (1, 0) in find_cancellations([st])


def test_window_limit():
    st = _s(1, [_t("2026-07-01", "X", 10.0), _t("2026-09-01", "X", -10.0)])
    assert find_cancellations([st]) == {}
    assert find_cancellations([st], window_days=90) != {}


def test_one_refund_cancels_only_one_duplicate_charge_nearest_date():
    st = _s(
        1,
        [
            _t("2026-09-01", "X", 10.0),
            _t("2026-09-07", "X", 10.0),
            _t("2026-09-08", "X", -10.0),
        ],
    )
    out = find_cancellations([st])
    assert out[(1, 2)] == (1, 1)
    assert (1, 0) not in out


def test_locked_charge_is_skipped():
    st = _s(1, [_t("2026-09-08", "X", 10.0), _t("2026-09-08", "X", -10.0)])
    assert find_cancellations([st], locked={(1, 0)}) == {}
    assert locked_from_matches(
        [{"statement_id": 1, "transaction_index": 0}, {"statement_id": 1}]
    ) == {(1, 0)}


def test_cross_statement_pairing_prefers_same_statement():
    a = _s(1, [_t("2026-08-20", "X", 10.0)])
    b = _s(
        2, [_t("2026-09-05", "X", 10.0), _t("2026-09-06", "X", -10.0), _t("2026-09-07", "X", -10.0)]
    )
    out = find_cancellations([a, b])
    # same-statement first: b's charge takes the nearest credit in b
    assert out[(2, 0)] == (2, 1)
    # the leftover credit cancels the charge on the earlier statement
    assert out[(1, 0)] == (2, 2)
    info = partner_info(out[(1, 0)], {1: a, 2: b})
    assert info == {
        "statement_id": 2,
        "index": 2,
        "date": "2026-09-07",
        "statement_filename": "fatura_2.pdf",
    }


def test_bad_rows_ignored():
    st = _s(1, [_t(None, "X", 10.0), _t("2026-09-08", "X", "abc"), _t("2026-09-08", "X", 0)])
    assert find_cancellations([st]) == {}
    assert partner_info(None, {}) is None
