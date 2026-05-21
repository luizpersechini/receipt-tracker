"""Tests for the Uber rider CSV importer (Phase A integration)."""

from pathlib import Path

from src.services.uber_csv import is_uber_csv, parse_uber_csv


def _write_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_is_uber_csv_detects_canonical_headers(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Begin Trip Address,Dropoff Address,Fare Amount,Fare Currency,City,Payment Method\n"
        "abc-123,2026-04-25 10:00:00,A St,B Ave,32.29,CAD,Vancouver,Visa\n"
    )
    assert is_uber_csv(str(csv))


def test_is_uber_csv_rejects_unrelated_csv(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Date,Description,Amount\n2026-04-25,coffee,5.00\n"
    )
    assert not is_uber_csv(str(csv))


def test_parse_uber_csv_basic_row(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Begin Trip Address,Dropoff Address,Fare Amount,Fare Currency,City,Payment Method\n"
        "abc-123,2026-04-25 10:00:00,Hotel,Airport,32.29,CAD,Vancouver,Visa ****4321\n"
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    assert len(rows) == 1
    r = rows[0]
    assert r.trip_id == "abc-123"
    assert r.date == "2026-04-25"
    assert r.fare == 32.29
    assert r.currency == "CAD"
    assert r.city == "Vancouver"
    assert r.begin_address == "Hotel"
    assert r.end_address == "Airport"


def test_metadata_shape_matches_ocr_output(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Begin Trip Address,Dropoff Address,Fare Amount,Fare Currency,City,Payment Method\n"
        "xyz,2026-04-25 10:00:00,A,B,12.50,USD,SF,Amex\n"
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    md = rows[0].to_metadata()
    # Same field names the OCR pipeline emits, so the rest of the app
    # (renderer, report generator, FX converter) just works.
    for field in ("date", "vendor_name", "total_amount", "currency",
                  "category", "vendor_address", "payment_method", "items"):
        assert field in md
    assert md["vendor_name"] == "Uber"
    assert md["category"] == "transportation"
    assert md["vendor_address"] == "A → B"
    assert md["uber_trip_id"] == "xyz"


def test_skips_cancelled_rides(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Fare Amount,Trip Status\n"
        "ok-1,2026-04-25 10:00:00,10.00,COMPLETED\n"
        "cancel-1,2026-04-25 11:00:00,0.00,CANCELED\n"
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    assert [r.trip_id for r in rows] == ["ok-1"]


def test_skips_rows_with_no_fare(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Fare Amount\n"
        "free-1,2026-04-25 10:00:00,0\n"
        "free-2,2026-04-25 11:00:00,\n"
        "paid-1,2026-04-25 12:00:00,15.00\n"
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    assert [r.trip_id for r in rows] == ["paid-1"]


def test_business_only_filter_keeps_business_rides(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Fare Amount,Payment Method,Expense Code\n"
        "biz-1,2026-04-25 10:00:00,10.00,Visa,Business\n"
        "pers-1,2026-04-25 11:00:00,12.00,Visa,\n"
        "biz-2,2026-04-25 12:00:00,8.00,Corp Card,\n"
    )
    rows = parse_uber_csv(str(csv), business_only=True)
    ids = {r.trip_id for r in rows}
    assert ids == {"biz-1", "biz-2"}


def test_handles_european_comma_decimal_amounts(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Fare Amount,Fare Currency\n"
        "eu-1,2026-04-25 10:00:00,\"12,50\",EUR\n"
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    assert rows[0].fare == 12.50
    assert rows[0].currency == "EUR"


def test_parse_raises_on_non_uber_csv(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Date,Description,Amount\n2026-04-25,coffee,5.00\n"
    )
    import pytest
    with pytest.raises(ValueError, match="begin-time"):
        parse_uber_csv(str(csv))


# ─── Real Uber data-download format (trips_data-0.csv) ─────────────────────

_REAL_HEADER = (
    "city_name,currency_code,timezone,flow,product_type_name,global_product_name,"
    "request_timestamp_local,request_timestamp_utc,request_lat,request_lng,"
    "begintrip_timestamp_local,begintrip_timestamp_utc,begintrip_lat,begintrip_lng,"
    "begintrip_address,dropoff_timestamp_local,dropoff_timestamp_utc,dropoff_lat,"
    "dropoff_lng,destination_lat,destination_lng,has_destination,dropoff_address,"
    "eta,surge_multiplier,is_surged,is_pool_matched,request_to_begin_distance_miles,"
    "request_to_begin_duration_seconds,trip_distance_miles,trip_duration_seconds,"
    "status,is_completed,fare_amount,is_fare_split,is_flat_rate,is_cash_trip,"
    "client_app_version,client_device,promotion_local,promotion_usd,credits_local,"
    "credits_usd,has_client_upfront_fare,client_upfront_fare_local,"
    "client_upfront_fare_usd,original_fare_local,original_fare_usd,base_fare_local,"
    "base_fare_usd,profile_type,card_number"
)


def _real_row(*, status="completed", begin="2026-04-25T10:00:00.000Z",
              addr="Hotel", drop_addr="Airport", upfront=32.29, currency="CAD",
              city="Vancouver", profile="BANK_CARD", card="•••• 4321", lat="49.2", lng="-123.1"):
    return (
        f"{city},{currency},UTC,P2P,UberX,UberX,"
        f"{begin},{begin},,,"
        f"{begin},{begin},{lat},{lng},"
        f'"{addr}",{begin},{begin},,,,,true,"{drop_addr}",'
        f",1.0,false,false,,,5.0,1200,"
        f"{status},true,,false,false,false,"
        f",,,,,,true,{upfront},5.29,,,,,,{profile},{card}"
    )


def test_is_uber_csv_detects_real_data_download_format(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv", _REAL_HEADER + "\n" + _real_row() + "\n")
    assert is_uber_csv(str(csv))


def test_real_format_parses_amount_from_client_upfront_local(tmp_path: Path):
    """fare_amount is empty in real exports — the actual paid amount lives
    in client_upfront_fare_local."""
    csv = _write_csv(tmp_path / "u.csv",
                     _REAL_HEADER + "\n" + _real_row(upfront=87.50) + "\n")
    rows = parse_uber_csv(str(csv), business_only=False)
    assert len(rows) == 1
    assert rows[0].fare == 87.50
    assert rows[0].currency == "CAD"


def test_real_format_synthesizes_stable_trip_id(tmp_path: Path):
    """Real CSV has no trip_id column. Re-parsing the same row must yield
    the same synthetic id (otherwise dedup is broken)."""
    body = _REAL_HEADER + "\n" + _real_row(begin="2026-04-25T10:00:00.000Z", upfront=32.29) + "\n"
    csv = _write_csv(tmp_path / "u.csv", body)
    rows1 = parse_uber_csv(str(csv), business_only=False)
    rows2 = parse_uber_csv(str(csv), business_only=False)
    assert rows1[0].trip_id == rows2[0].trip_id
    assert rows1[0].trip_id.startswith("syn-")


def test_real_format_skips_rider_canceled(tmp_path: Path):
    body = _REAL_HEADER + "\n"
    body += _real_row(status="completed",     begin="2026-04-25T10:00:00.000Z", upfront=10.0) + "\n"
    body += _real_row(status="rider_canceled", begin="2026-04-25T11:00:00.000Z", upfront=5.0) + "\n"
    body += _real_row(status="driver_canceled",begin="2026-04-25T12:00:00.000Z", upfront=5.0) + "\n"
    body += _real_row(status="unfulfilled",    begin="2026-04-25T13:00:00.000Z", upfront=5.0) + "\n"
    csv = _write_csv(tmp_path / "u.csv", body)
    rows = parse_uber_csv(str(csv), business_only=False)
    assert len(rows) == 1


def test_date_range_filter_keeps_only_matching_rides(tmp_path: Path):
    body = _REAL_HEADER + "\n"
    body += _real_row(begin="2026-03-31T10:00:00.000Z", upfront=10.0) + "\n"   # out
    body += _real_row(begin="2026-04-01T10:00:00.000Z", upfront=20.0) + "\n"   # in
    body += _real_row(begin="2026-04-15T10:00:00.000Z", upfront=30.0) + "\n"   # in
    body += _real_row(begin="2026-04-30T10:00:00.000Z", upfront=40.0) + "\n"   # in
    body += _real_row(begin="2026-05-01T10:00:00.000Z", upfront=50.0) + "\n"   # out
    csv = _write_csv(tmp_path / "u.csv", body)
    rows = parse_uber_csv(str(csv), business_only=False,
                          after="2026-04-01", before="2026-04-30")
    assert [r.fare for r in rows] == [20.0, 30.0, 40.0]


def test_real_format_extracts_addresses_and_city(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        _REAL_HEADER + "\n" + _real_row(addr="Hotel Lobby", drop_addr="YVR Airport",
                                         city="Vancouver") + "\n",
    )
    rows = parse_uber_csv(str(csv), business_only=False)
    assert rows[0].begin_address == "Hotel Lobby"
    assert rows[0].end_address == "YVR Airport"
    assert rows[0].city == "Vancouver"


def test_synthetic_filename_is_stable_for_dedup(tmp_path: Path):
    csv = _write_csv(tmp_path / "u.csv",
        "Trip or Order ID,Begin Trip Time,Fare Amount\n"
        "stable-id-42,2026-04-25 10:00:00,7.50\n"
    )
    rows1 = parse_uber_csv(str(csv), business_only=False)
    rows2 = parse_uber_csv(str(csv), business_only=False)
    assert rows1[0].synthetic_filename() == rows2[0].synthetic_filename()
    assert rows1[0].synthetic_filename() == "uber_stable-id-42.csv"
