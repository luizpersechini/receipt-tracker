"""Tests for the Uber-email HTML parser (Phase B).

These tests cover the *parsing* side only — no Gmail API calls. The samples
are intentionally minimal: stripped-down representations of the patterns
Uber's templates use.
"""

from src.services.gmail_ingest import build_query, parse_uber_email


# ─── Sample bodies ───────────────────────────────────────────────────────────

SAMPLE_BUSINESS = """\
<html><body>
  <h1>Thanks for riding, Luiz</h1>
  <p>Here's a copy of your receipt for your <strong>Business</strong> trip.</p>
  <table>
    <tr><td>Total</td><td>CA$ 32.29</td></tr>
  </table>
  <p>10:15 AM<br/>YVR Airport - International Departures<br/>10:48 AM<br/>Whistler Hotel Lobby</p>
  <p>Payment: Visa ••••4321</p>
  <a href="https://riders.uber.com/trips/abcd-1234-5678">View receipt</a>
</body></html>
"""

SAMPLE_PERSONAL = """\
<html><body>
  <h1>Thanks for riding</h1>
  <p>Total $18.50</p>
  <p>8:23 PM<br/>123 Main St<br/>8:39 PM<br/>456 Oak Ave</p>
  <p>Mastercard ••••9876</p>
  <a href="https://riders.uber.com/trips/personal-trip-id">Receipt</a>
</body></html>
"""

SAMPLE_BRL = """\
<html><body>
  <p>Total R$ 41,78</p>
  <p>19:02<br/>Av. Paulista, 1000<br/>19:18<br/>R. Augusta, 500</p>
  <p>Visa ••••5555</p>
  <a href="https://riders.uber.com/trips/brl-trip-uuid">Receipt</a>
</body></html>
"""


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_parses_amount_and_currency_from_canadian_business_receipt():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip with Uber",
        date_header="Sat, 25 Apr 2026 10:48:00 -0700",
        html=SAMPLE_BUSINESS,
        plain=None,
    )
    assert p.total_amount == 32.29
    assert p.currency == "CAD"
    assert p.is_business is True
    assert p.payment_method and "Visa" in p.payment_method
    assert p.trip_id == "abcd-1234-5678"


def test_resolves_date_from_email_header_when_subject_lacks_it():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip with Uber",
        date_header="Sat, 25 Apr 2026 10:48:00 -0700",
        html=SAMPLE_BUSINESS,
        plain=None,
    )
    assert p.date == "2026-04-25"


def test_extracts_pickup_and_dropoff_from_time_blocks():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip",
        date_header=None,
        html=SAMPLE_BUSINESS,
        plain=None,
    )
    assert p.pickup and "YVR" in p.pickup
    assert p.dropoff and "Whistler" in p.dropoff


def test_personal_ride_not_flagged_business():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip with Uber",
        date_header="Sun, 26 Apr 2026 20:39:00 -0700",
        html=SAMPLE_PERSONAL,
        plain=None,
    )
    assert p.total_amount == 18.50
    assert p.currency == "USD"
    assert p.is_business is False


def test_parses_brazilian_real_with_comma_decimal():
    p = parse_uber_email(
        msg_id="m1",
        subject="Sua viagem com a Uber",
        date_header="Mon, 27 Apr 2026 19:18:00 -0300",
        html=SAMPLE_BRL,
        plain=None,
    )
    assert p.total_amount == 41.78
    assert p.currency == "BRL"


def test_synthetic_filename_uses_trip_uuid_when_available():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip", date_header=None,
        html=SAMPLE_BUSINESS, plain=None,
    )
    assert p.synthetic_filename() == "uber_abcd-1234-5678.eml"


def test_synthetic_filename_falls_back_to_msg_id_when_trip_uuid_missing():
    """Real Uber emails wrap trip links in click-trackers, so we usually
    can't pull the uuid. Gmail message-id is a stable per-email dedup key."""
    html_no_uuid = """\
    <html><body>
      <p>Apr 17, 2026</p>
      <p>Total R$18.98</p>
      <a href="https://email.uber.com/wf/click?upn=opaque-tracker-id">
        View receipt
      </a>
    </body></html>
    """
    p = parse_uber_email(
        msg_id="19e18926c93e7f27",
        subject="[Business] Your Friday afternoon trip with Uber",
        date_header="Mon, 11 May 2026 19:45:09 +0000",
        html=html_no_uuid, plain=None,
    )
    assert p.trip_id is None
    assert p.synthetic_filename() == "uber_gm-19e18926c93e7f27.eml"


def test_prefers_body_date_over_email_header():
    """Email header has the resend date; body has the actual ride date."""
    html = """\
    <html><body>
      <p>Apr 17, 2026</p>
      <p>12:01 p.m.</p>
      <p>Total R$18.98</p>
    </body></html>
    """
    p = parse_uber_email(
        msg_id="m1",
        subject="[Business] Your trip with Uber",
        date_header="Mon, 11 May 2026 19:45:09 +0000",
        html=html, plain=None,
    )
    assert p.date == "2026-04-17"


def test_extracts_portuguese_body_date():
    html = "<html><body><p>17 de abril de 2026</p><p>Total R$18.98</p></body></html>"
    p = parse_uber_email(
        msg_id="m2",
        subject="Sua viagem com a Uber",
        date_header=None, html=html, plain=None,
    )
    assert p.date == "2026-04-17"


def test_business_detected_from_bracketed_subject_prefix():
    p = parse_uber_email(
        msg_id="m1",
        subject="[Business] Your Friday afternoon trip with Uber",
        date_header=None, html="<p>Total R$10</p>", plain=None,
    )
    assert p.is_business is True


def test_metadata_shape_matches_ocr_output():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip",
        date_header="Sat, 25 Apr 2026 10:48:00 -0700",
        html=SAMPLE_BUSINESS,
        plain=None,
    )
    md = p.to_metadata()
    # Same field names as OCR / CSV path — downstream code is generic
    for field_ in ("date", "vendor_name", "total_amount", "currency",
                   "category", "vendor_address", "payment_method", "items"):
        assert field_ in md
    assert md["vendor_name"] == "Uber"
    assert md["category"] == "transportation"
    assert md["uber_source"] == "gmail"


def test_handles_email_with_no_html_body_gracefully():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your trip",
        date_header=None,
        html=None,
        plain="Total $5.00\nVisa ••••1234",
    )
    # Even from plaintext we should recover the amount
    assert p.total_amount == 5.00
    assert p.currency == "USD"


def test_build_query_no_filters():
    assert build_query("from:noreply@uber.com") == "from:noreply@uber.com"


def test_build_query_with_after_only():
    q = build_query("from:noreply@uber.com", after="2024-01-01")
    assert q == "from:noreply@uber.com after:2024/01/01"


def test_build_query_before_is_made_inclusive():
    """Gmail's before: is exclusive — we bump by a day so users can pass the
    end of the range inclusively."""
    q = build_query("from:noreply@uber.com", before="2024-12-31")
    assert q == "from:noreply@uber.com before:2025/01/01"


def test_build_query_combines_label_and_date_range():
    q = build_query(
        "from:noreply@uber.com",
        after="2024-06-01", before="2024-06-30",
        exclude_label="rt-processed",
    )
    assert q == "from:noreply@uber.com -label:rt-processed after:2024/06/01 before:2024/07/01"


def test_build_query_ignores_malformed_dates():
    """Garbage in, no garbage out — bad dates are dropped, not stuffed in."""
    q = build_query("from:noreply@uber.com", after="not-a-date", before="2024")
    assert q == "from:noreply@uber.com"


# ─── Gmail error categoriser ───────────────────────────────────────────────

def test_categorise_project_deleted_error():
    from src.services.gmail_ingest import _categorise_gmail_error
    err = Exception("Project #418165347312 has been deleted.")
    out = _categorise_gmail_error(err)
    assert out["success"] is False
    assert out["error_kind"] == "project_deleted"
    assert "deleted" in out["error"].lower() or "project" in out["error"].lower()


def test_categorise_invalid_client():
    from src.services.gmail_ingest import _categorise_gmail_error
    out = _categorise_gmail_error(Exception("oauth2 invalid_client"))
    assert out["error_kind"] == "invalid_client"


def test_categorise_token_expired():
    from src.services.gmail_ingest import _categorise_gmail_error
    out = _categorise_gmail_error(Exception("invalid_grant: Token has been expired or revoked."))
    assert out["error_kind"] == "token_expired"


def test_categorise_api_disabled():
    from src.services.gmail_ingest import _categorise_gmail_error
    out = _categorise_gmail_error(Exception("Gmail API has not been used in project 123 before or it is disabled"))
    assert out["error_kind"] == "api_disabled"


def test_categorise_unknown_falls_back():
    from src.services.gmail_ingest import _categorise_gmail_error
    out = _categorise_gmail_error(Exception("something else entirely"))
    assert out["error_kind"] == "unknown"


# ─── /api/gmail/credentials upload endpoint ────────────────────────────────

def _client_for_credentials_upload(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.config import settings
    monkeypatch.setattr(settings, "gmail_credentials_path", tmp_path / "credentials.json")
    monkeypatch.setattr(settings, "gmail_token_path", tmp_path / "token.json")
    # Import main lazily so the patched settings are in effect at app build time
    from src.main import app
    return TestClient(app)


def test_credentials_upload_accepts_valid_desktop_client(tmp_path, monkeypatch):
    client = _client_for_credentials_upload(tmp_path, monkeypatch)
    payload = (
        '{"installed":{"client_id":"new-cid","client_secret":"new-secret",'
        '"redirect_uris":["http://localhost"],'
        '"auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token"}}'
    )
    resp = client.post(
        "/api/gmail/credentials",
        files={"file": ("creds.json", payload, "application/json")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["client_id"] == "new-cid"
    from src.config import settings
    assert settings.gmail_credentials_path.exists()


def test_credentials_upload_drops_stale_token(tmp_path, monkeypatch):
    client = _client_for_credentials_upload(tmp_path, monkeypatch)
    from src.config import settings
    # Pre-existing token from the old (now-defunct) client
    settings.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.gmail_token_path.write_text("stale-token")
    payload = (
        '{"installed":{"client_id":"new-cid","client_secret":"s",'
        '"redirect_uris":["http://localhost"]}}'
    )
    resp = client.post(
        "/api/gmail/credentials",
        files={"file": ("creds.json", payload, "application/json")},
    )
    assert resp.status_code == 200
    assert not settings.gmail_token_path.exists()  # token wiped


def test_credentials_upload_rejects_non_json(tmp_path, monkeypatch):
    client = _client_for_credentials_upload(tmp_path, monkeypatch)
    resp = client.post(
        "/api/gmail/credentials",
        files={"file": ("creds.json", "this is not json", "application/json")},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"]


def test_credentials_upload_rejects_wrong_shape(tmp_path, monkeypatch):
    """A JSON file without `installed.client_id` is not an OAuth Desktop client."""
    client = _client_for_credentials_upload(tmp_path, monkeypatch)
    resp = client.post(
        "/api/gmail/credentials",
        files={"file": ("creds.json", '{"web":{"client_id":"x"}}', "application/json")},
    )
    assert resp.status_code == 400
    assert "installed" in resp.json()["detail"].lower()


def test_returns_none_amount_when_no_total_in_body():
    p = parse_uber_email(
        msg_id="m1",
        subject="Your Uber account",
        date_header=None,
        html="<p>Hello, you have a new login.</p>",
        plain=None,
    )
    assert p.total_amount is None
