"""
Safety tests for the OCR pipeline.

These tests guarantee that the receipt-upload flow CANNOT silently load a
huge vision model or push a multi-megapixel image into Ollama. Both of those
are what froze the dev machine the first time we tried the end-to-end flow.
"""

from io import BytesIO

import pytest
from PIL import Image

from src.config import settings
from src.services import llm_processor as llm


GB = 1024**3


@pytest.fixture(autouse=True)
def _isolate_ocr_override(monkeypatch):
    """`_pick_model` now reads the runtime model via `ocr_settings`. Pin it to
    `settings.ollama_model` so the persisted override file (data/ocr_config.json,
    set by the sidebar picker) can't shadow the values these tests monkeypatch."""
    monkeypatch.setattr(llm.ocr_settings, "effective_vision_model", lambda: settings.ollama_model)


# ─── _pick_model safety rails ────────────────────────────────────────────────


def test_picks_configured_model_when_installed_and_under_cap(monkeypatch):
    monkeypatch.setattr(settings, "ollama_model", "glm-ocr:latest")
    monkeypatch.setattr(settings, "ocr_max_model_gb", 6.0)
    available = {
        "glm-ocr:latest": 2 * GB,
        "llama3:latest": 4 * GB,  # text-only, must be ignored
        "gemma4:latest": 9 * GB,  # over the cap, must be ignored
    }
    assert llm._pick_model(available=available) == "glm-ocr:latest"


def test_refuses_model_over_size_cap(monkeypatch):
    """The configured model itself is too big — picker must NOT use it."""
    monkeypatch.setattr(settings, "ollama_model", "gemma4:latest")
    monkeypatch.setattr(settings, "ocr_max_model_gb", 6.0)
    available = {"gemma4:latest": 9 * GB}
    with pytest.raises(llm.OCRConfigError, match="over the.*GB cap"):
        llm._pick_model(available=available)


def test_refuses_when_only_non_vision_models_installed(monkeypatch):
    """Text-only models like llama3 must NEVER be picked, even as a fallback."""
    monkeypatch.setattr(settings, "ollama_model", "glm-ocr:latest")
    monkeypatch.setattr(settings, "ocr_max_model_gb", 6.0)
    available = {
        "llama3:latest": 4 * GB,
        "llama3.1:8b": 5 * GB,
    }
    with pytest.raises(llm.OCRConfigError, match="No safe vision model"):
        llm._pick_model(available=available)


def test_falls_back_to_smallest_allowlisted_vision_model(monkeypatch):
    """If the configured model isn't installed, prefer a small vision model."""
    monkeypatch.setattr(settings, "ollama_model", "glm-ocr:latest")
    monkeypatch.setattr(settings, "ocr_max_model_gb", 6.0)
    available = {
        "llava:latest": 5 * GB,
        "moondream:latest": 2 * GB,
        "llama3:latest": 4 * GB,
    }
    assert llm._pick_model(available=available) == "moondream:latest"


def test_refuses_when_ollama_returns_no_models(monkeypatch):
    """Ollama down / no models pulled = loud error, no silent default."""
    with pytest.raises(llm.OCRConfigError, match="unreachable or has no installed"):
        llm._pick_model(available={})


def test_rejects_unknown_family_even_if_under_cap(monkeypatch):
    """A random model not in the allowlist must not be picked."""
    monkeypatch.setattr(settings, "ollama_model", "glm-ocr:latest")
    monkeypatch.setattr(settings, "ocr_max_model_gb", 6.0)
    available = {"some-random-model:latest": 1 * GB}
    with pytest.raises(llm.OCRConfigError):
        llm._pick_model(available=available)


# ─── _resize_for_ocr — guard against pushing 12 MP images to Ollama ─────────


def test_resize_shrinks_large_image_to_max_dim():
    big = Image.new("RGB", (4032, 3024), color="white")
    out = llm._resize_for_ocr(big, max_dim=1536)
    longest = max(out.size)
    assert longest <= 1536
    # Aspect ratio preserved within 1px rounding
    assert abs(out.size[0] / out.size[1] - 4032 / 3024) < 0.01


def test_resize_leaves_small_image_untouched():
    small = Image.new("RGB", (800, 600), color="white")
    out = llm._resize_for_ocr(small, max_dim=1536)
    assert out.size == (800, 600)


def test_resize_handles_portrait_aspect_ratio():
    portrait = Image.new("RGB", (3024, 4032), color="white")
    out = llm._resize_for_ocr(portrait, max_dim=1024)
    assert max(out.size) <= 1024
    # Should still be taller than wide
    assert out.size[1] > out.size[0]


# ─── _refine_currency — country-aware fallback ─────────────────────────────


def test_currency_filled_from_country_when_missing():
    """No currency detected on receipt → use country's currency."""
    assert llm._refine_currency(None, "CA") == "CAD"
    assert llm._refine_currency(None, "BR") == "BRL"
    assert llm._refine_currency(None, "FR") == "EUR"


def test_currency_usd_overridden_by_country():
    """LLM defaulted to USD because of bare $; country fixes it."""
    assert llm._refine_currency("USD", "CA") == "CAD"
    assert llm._refine_currency("USD", "AU") == "AUD"
    assert llm._refine_currency("USD", "NZ") == "NZD"
    assert llm._refine_currency("USD", "MX") == "MXN"


def test_currency_usd_kept_when_country_is_us():
    assert llm._refine_currency("USD", "US") == "USD"


def test_country_currency_kept_when_already_matches():
    """When country and currency already agree, output equals input."""
    assert llm._refine_currency("BRL", "BR") == "BRL"
    assert llm._refine_currency("EUR", "DE") == "EUR"
    assert llm._refine_currency("GBP", "GB") == "GBP"


def test_country_trumps_currency_even_when_currency_looks_unambiguous():
    """A Vancouver receipt that the model hallucinated as BRL must end up CAD.
    Country is the higher-confidence signal — the model reads place names
    much more reliably than currency symbols."""
    assert llm._refine_currency("BRL", "CA") == "CAD"
    assert llm._refine_currency("EUR", "BR") == "BRL"
    assert llm._refine_currency("USD", "GB") == "GBP"


# ─── _normalize_country — coerce whatever the LLM returns to ISO-2 ─────────


def test_normalize_country_accepts_iso2_verbatim():
    assert llm._normalize_country("CA") == "CA"
    assert llm._normalize_country("ca") == "CA"
    assert llm._normalize_country("br") == "BR"


def test_normalize_country_resolves_full_name():
    """The model often ignores the ISO-2 instruction; we recover."""
    assert llm._normalize_country("Canada") == "CA"
    assert llm._normalize_country("Brazil") == "BR"
    assert llm._normalize_country("Brasil") == "BR"
    assert llm._normalize_country("United States") == "US"
    assert llm._normalize_country("U.S.A.") == "US"
    assert llm._normalize_country("United Kingdom") == "GB"


def test_normalize_country_handles_decorations():
    assert llm._normalize_country("(Canada)") == "CA"
    assert llm._normalize_country("Vancouver, Canada") == "CA"


def test_normalize_country_promotes_province_codes_to_country():
    """The model sometimes returns the province / state code instead of the
    country code ("Vancouver BC" → it picks 'BC'). Promote to the parent
    country so currency refinement still works."""
    assert llm._normalize_country("BC") == "CA"
    assert llm._normalize_country("QC") == "CA"
    assert llm._normalize_country("ON") == "CA"
    assert llm._normalize_country("SP") == "BR"
    assert llm._normalize_country("MG") == "BR"


# ─── Post-processing fallbacks reading raw OCR text ─────────────────────────


def test_scan_currency_finds_explicit_cad_marker():
    """Vancouver receipts often print 'CAD$' or 'CA$' explicitly — that
    should override whatever the LLM guessed."""
    assert llm._scan_currency_in_text("TOTAL CAD$ 38.10") == "CAD"
    assert llm._scan_currency_in_text("Total CA$ 12.50") == "CAD"
    assert llm._scan_currency_in_text("Net R$ 18,98 (BRL)") == "BRL"
    assert llm._scan_currency_in_text("$5.99 only here") is None or "USD"
    assert llm._scan_currency_in_text("") is None


def test_scan_date_finds_common_patterns():
    assert llm._scan_date_in_text("Date: 2026-04-30 thank you") == "2026-04-30"
    assert llm._scan_date_in_text("10/03/2026 11:54am") == "10/03/2026"
    assert llm._scan_date_in_text("Mar 10, 2026 at 1:44pm") == "Mar 10, 2026"
    assert llm._scan_date_in_text("no date here") is None


# ─── _strip_time_from_date — keep only calendar date ───────────────────────


def test_strip_time_removes_seconds_with_ampm():
    """Trees Coffee Gastown prints '10-Mar-2026 1:44:53p.m.' — drop the time."""
    assert llm._strip_time_from_date("10-Mar-2026 1:44:53p.m.") == "10-Mar-2026"


def test_strip_time_removes_simple_hhmm_with_ampm():
    """Ciclo Espresso prints '2026-03-10 10:29 a.m.' — drop the time."""
    assert llm._strip_time_from_date("2026-03-10 10:29 a.m.") == "2026-03-10"
    assert llm._strip_time_from_date("2026-03-10 9:56 a.m.") == "2026-03-10"


def test_strip_time_removes_at_clock():
    """'Mar 10, 2026 at 1:44pm' → 'Mar 10, 2026'."""
    assert llm._strip_time_from_date("Mar 10, 2026 at 1:44pm") == "Mar 10, 2026"


def test_strip_time_removes_24h_clock():
    """24-hour clock without am/pm marker."""
    assert llm._strip_time_from_date("2026-03-10 14:30:00") == "2026-03-10"
    assert llm._strip_time_from_date("16/03/2026 08:50") == "16/03/2026"


def test_strip_time_leaves_clean_date_alone():
    assert llm._strip_time_from_date("2026-03-10") == "2026-03-10"
    assert llm._strip_time_from_date("30/04/2026") == "30/04/2026"
    assert llm._strip_time_from_date("Mar 10, 2026") == "Mar 10, 2026"


def test_strip_time_handles_empty_and_none():
    assert llm._strip_time_from_date(None) is None
    assert llm._strip_time_from_date("") is None
    assert llm._strip_time_from_date("   ") is None


def test_strip_time_chops_trailing_comma_or_dash():
    assert llm._strip_time_from_date("10-Mar-2026 ") == "10-Mar-2026"
    assert llm._strip_time_from_date("Mar 10, 2026,") == "Mar 10, 2026"


# ─── _scan_total_in_text — pick the final-charged amount ───────────────────


def test_scan_total_prefers_final_charged_over_pre_tip_total():
    """Trees Coffee Gastown: printed 'Total $13.97' followed by 'VENTA: CRÉDITO
    $16.46'. The VENTA line is what the card was actually charged."""
    text = (
        "Subtotal           $13.30\n"
        "GST 5%             $0.67\n"
        "Total              $13.97\n"
        "Propina            $2.10\n"
        "Credit card surcharge 2.4% $0.39\n"
        "VENTA: CRÉDITO     $16.46\n"
        "VISA 2928\n"
    )
    assert llm._scan_total_in_text(text) == 16.46


def test_scan_total_picks_total_cad_over_amount():
    """SSP America Whistler Brewing: 'Amount $32.29' then 'TOTAL CAD$38.10'.
    TOTAL CAD$ is the final charged amount."""
    text = "Amount        $32.29\nTip           $5.81\nTOTAL CAD$    38.10\n"
    assert llm._scan_total_in_text(text) == 38.10


def test_scan_total_returns_plain_total_when_no_final_marker():
    """Ciclo Espresso check #30: receipt only has Subtotal / Total — no
    VENTA / Amount Charged line. We should still pick the Total line."""
    text = (
        "1 12oz Americano   $5.75\n"
        "Subtotal           $5.75\n"
        "GST                $0.29\n"
        "Tip                $1.08\n"
        "Total              $7.12\n"
    )
    assert llm._scan_total_in_text(text) == 7.12


def test_scan_total_ignores_subtotal_tax_tip_lines():
    """A subtotal/tip/tax line must NEVER masquerade as the total even if
    nothing else is labeled."""
    text = "Subtotal $99.00\nTax $7.92\nTip $14.00\n"
    assert llm._scan_total_in_text(text) is None


def test_scan_total_handles_comma_decimal_locale():
    """Brazilian receipts: 'Total R$ 41,78' should resolve to 41.78."""
    text = "Padaria · Pão de queijo R$ 12,00\nTotal R$ 41,78\n"
    assert llm._scan_total_in_text(text) == 41.78


def test_scan_total_returns_none_for_empty_or_nothing_useful():
    assert llm._scan_total_in_text("") is None
    assert llm._scan_total_in_text(None) is None
    assert llm._scan_total_in_text("just some text no money") is None


def test_post_process_overrides_undersized_total_with_scanned_final():
    """LLM picked the subtotal — post-processing rescues with VENTA line."""
    data = {"vendor_name": "Trees", "total_amount": 13.97, "currency": "CAD"}
    text = "Total      $13.97\nPropina    $2.10\nVENTA: CRÉDITO  $16.46\n"
    llm._post_process_with_ocr_text(data, text)
    assert data["total_amount"] == 16.46


def test_post_process_keeps_total_when_scanned_is_smaller():
    """If the LLM total is larger or equal to what we can find in OCR,
    leave it alone — don't downgrade a correct value."""
    data = {"vendor_name": "X", "total_amount": 50.00, "currency": "USD"}
    text = "Subtotal $30.00\nTotal $35.00\n"
    llm._post_process_with_ocr_text(data, text)
    assert data["total_amount"] == 50.00


def test_post_process_fills_missing_total_from_scan():
    """LLM emitted null total — scan recovers it."""
    data = {"vendor_name": "X", "total_amount": None, "currency": "USD"}
    text = "Subtotal $30.00\nTax $2.00\nTotal $32.00\n"
    llm._post_process_with_ocr_text(data, text)
    assert data["total_amount"] == 32.00


def test_post_process_fills_missing_date():
    data = {"vendor_name": "X", "date": None, "currency": "CAD", "category": "meals"}
    llm._post_process_with_ocr_text(data, "Trees Coffee · 10-Mar-2026 1:44p.m.")
    assert data["date"] is not None
    assert "Mar" in data["date"] or "10" in data["date"]


def test_post_process_overrides_currency_from_explicit_marker():
    """LLM said BRL but the OCR text literally contains 'CAD$' — currency
    flips to CAD."""
    data = {"vendor_name": "Trees Coffee", "currency": "BRL", "category": "meals"}
    llm._post_process_with_ocr_text(data, "VISA Purchase TOTAL CAD$ 13.97 Vancouver BC")
    assert data["currency"] == "CAD"


def test_post_process_currency_does_not_change_when_matching():
    data = {"vendor_name": "Padaria", "currency": "BRL", "category": "meals"}
    llm._post_process_with_ocr_text(data, "Total R$ 12,34")
    assert data["currency"] == "BRL"


def test_post_process_rescues_other_category_via_keywords():
    """LLM defaulted to 'other' but the vendor name screams coffee shop."""
    data = {
        "vendor_name": "Trees Coffee Gastown",
        "currency": "CAD",
        "category": "other",
        "items": [],
    }
    llm._post_process_with_ocr_text(data, "Espresso · cappuccino · pastry")
    assert data["category"] == "meals"


def test_post_process_keeps_non_other_category_untouched():
    """If LLM said something specific, don't override even if other
    keywords pop up in the text."""
    data = {"vendor_name": "Uber", "currency": "USD", "category": "transportation", "items": []}
    llm._post_process_with_ocr_text(data, "ride · cafe stop on the way")
    assert data["category"] == "transportation"


def test_country_inferred_from_city_when_model_returns_none(monkeypatch):
    """When the model fails to emit country but extracts a recognisable city,
    we should still derive the right currency."""
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(settings, "ocr_backend", "ollama_vision")
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"vendor_name":"Trees Coffee","total_amount":"13.97","currency":"BRL",'
            '"date":"2026-04-30","city":"Vancouver","country":null}'
        ),
    )
    results = llm.process_receipt_image("/tmp/x.jpg")
    assert len(results) == 1
    # City "Vancouver" → country CA → currency overrides BRL to CAD
    assert results[0]["country"] == "CA"
    assert results[0]["currency"] == "CAD"


def test_country_inferred_from_accented_city():
    """São Paulo with accents should still resolve to BR."""
    assert llm._country_from_city("São Paulo") == "BR"
    assert llm._country_from_city("são paulo") == "BR"
    assert llm._country_from_city("Sao Paulo") == "BR"


def test_country_from_city_handles_compound_value():
    """City fields like 'Vancouver, BC' or 'Rio de Janeiro / RJ' still work."""
    assert llm._country_from_city("Vancouver, BC") == "CA"
    assert llm._country_from_city("Rio de Janeiro") == "BR"


def test_country_from_city_returns_none_for_unknown():
    assert llm._country_from_city(None) is None
    assert llm._country_from_city("") is None
    assert llm._country_from_city("Atlantis") is None


def test_country_falls_back_through_state_then_city(monkeypatch):
    """If both country AND state are null but city is set, city wins."""
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"vendor_name":"X","total_amount":"5","currency":"USD","date":"2026-05-01",'
            '"city":"Toronto","state":null,"country":null}'
        ),
    )
    results = llm.process_receipt_image("/tmp/x.jpg")
    assert results[0]["country"] == "CA"
    assert results[0]["currency"] == "CAD"


def test_country_state_wins_over_city_when_state_set(monkeypatch):
    """A subdivision code in `state` (e.g. 'BC') resolves before city — that
    way conflicting city names (rare) still go through the more specific
    geographic signal."""
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"vendor_name":"X","total_amount":"5","currency":"BRL","date":"2026-05-01",'
            '"city":"London","state":"BC","country":null}'  # London BC in Canada (unusual but possible)
        ),
    )
    results = llm.process_receipt_image("/tmp/x.jpg")
    assert results[0]["country"] == "CA"
    assert results[0]["currency"] == "CAD"


def test_subdivision_promotion_makes_currency_refinement_work():
    """End-to-end: a Vancouver receipt that the model tagged with country='BC'
    must still produce CAD after normalisation + refinement."""
    country = llm._normalize_country("BC")
    assert country == "CA"
    assert llm._refine_currency("BRL", country) == "CAD"


def test_normalize_country_returns_none_for_unknown():
    assert llm._normalize_country(None) is None
    assert llm._normalize_country("") is None
    assert llm._normalize_country("Atlantis") is None


def test_country_normalisation_makes_currency_refinement_work():
    """End-to-end: 'Canada' as a full name still produces CAD."""
    country = llm._normalize_country("Canada")
    assert country == "CA"
    assert llm._refine_currency("USD", country) == "CAD"


def test_currency_unchanged_when_country_unknown():
    assert llm._refine_currency("USD", None) == "USD"
    assert llm._refine_currency(None, None) is None
    assert llm._refine_currency("USD", "XX") == "USD"  # not in map


# ─── Multi-receipt parsing (single image, multiple receipts on the page) ───


def _stub_ollama_response(content: str):
    """Build a fake Ollama /api/chat response body wrapping the given content."""
    return {"message": {"content": content}}


def _stub_segmentation_to_single_image(monkeypatch):
    """Make the segmenter return a single 100x100 PIL image so the pipeline
    proceeds straight to OCR without needing real cv2 input.

    Also forces ocr_backend back to 'ollama_vision' because the project's
    .env may have been switched to 'apple_vision', which would route around
    the stubbed Ollama path these tests rely on."""
    monkeypatch.setattr(settings, "ocr_backend", "ollama_vision")
    monkeypatch.setattr(
        "src.services.receipt_segmenter.segment_receipts",
        lambda *_a, **_k: [Image.new("RGB", (100, 100), color="white")],
    )
    monkeypatch.setattr(llm, "_image_to_ocr_bytes", lambda *_a, **_k: b"pngbytes")


def test_process_returns_list_when_model_returns_receipts_array(monkeypatch, tmp_path):
    """The new prompt asks for {"receipts": [...]} — confirm we split them."""
    # Skip the picker safety rails by stubbing
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"receipts":['
            '{"vendor_name":"Cafe A","total_amount":"12.50","currency":"USD","date":"2026-05-01","category":"meals","country":"US"},'
            '{"vendor_name":"Pharmacy B","total_amount":"7.25","currency":"USD","date":"2026-05-01","category":"other","country":"US"}'
            "]}"
        ),
    )

    results = llm.process_receipt_image("/tmp/whatever.jpg")
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0]["vendor_name"] == "Cafe A"
    assert results[0]["total_amount"] == 12.50
    assert results[1]["vendor_name"] == "Pharmacy B"
    assert results[1]["total_amount"] == 7.25


def test_process_returns_list_for_back_compat_single_object(monkeypatch):
    """Older outputs may still return a bare receipt object — wrap into list."""
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"vendor_name":"Solo Vendor","total_amount":"99.99","currency":"USD","date":"2026-05-01","country":"US"}'
        ),
    )
    results = llm.process_receipt_image("/tmp/whatever.jpg")
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["vendor_name"] == "Solo Vendor"
    assert results[0]["total_amount"] == 99.99


def test_process_returns_single_error_row_on_parse_failure(monkeypatch):
    """When the model emits unparseable garbage, return [empty + error]."""
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(
        llm, "_ollama_chat", lambda *_a, **_k: _stub_ollama_response("this is not json at all")
    )
    results = llm.process_receipt_image("/tmp/whatever.jpg")
    assert isinstance(results, list)
    assert len(results) == 1
    assert "error" in results[0]


def test_process_returns_single_error_row_on_empty_receipts_array(monkeypatch):
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(
        llm, "_ollama_chat", lambda *_a, **_k: _stub_ollama_response('{"receipts": []}')
    )
    results = llm.process_receipt_image("/tmp/whatever.jpg")
    assert isinstance(results, list)
    assert len(results) == 1
    assert "error" in results[0]


def test_process_with_segments_returns_crop_per_receipt(monkeypatch):
    """When the segmenter splits the image, each receipt's result must carry
    its own crop image so the upload router can save per-receipt thumbnails."""
    crop_a = Image.new("RGB", (200, 400), color="white")
    crop_b = Image.new("RGB", (200, 400), color="lightgrey")
    monkeypatch.setattr(settings, "ocr_backend", "ollama_vision")
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    monkeypatch.setattr(
        "src.services.receipt_segmenter.segment_receipts",
        lambda *_a, **_k: [crop_a, crop_b],
    )
    monkeypatch.setattr(llm, "_image_to_ocr_bytes", lambda *_a, **_k: b"pngbytes")

    # Each segment returns its own single-receipt JSON
    responses = iter(
        [
            _stub_ollama_response(
                '{"receipts":[{"vendor_name":"A","total_amount":"10","currency":"USD","date":"2026-05-01"}]}'
            ),
            _stub_ollama_response(
                '{"receipts":[{"vendor_name":"B","total_amount":"20","currency":"USD","date":"2026-05-01"}]}'
            ),
        ]
    )
    monkeypatch.setattr(llm, "_ollama_chat", lambda *_a, **_k: next(responses))

    items = llm.process_with_segments("/tmp/x.jpg")
    assert len(items) == 2
    assert items[0]["metadata"]["vendor_name"] == "A"
    assert items[1]["metadata"]["vendor_name"] == "B"
    # Each item has its OWN crop — and they're the distinct PIL images we
    # injected, not None and not shared.
    assert items[0]["crop"] is crop_a
    assert items[1]["crop"] is crop_b


def test_process_with_segments_returns_none_crop_for_single_segment(monkeypatch):
    """When the segmenter finds only one segment (the whole image), crop is
    None — the upload router will reuse the original uploaded file."""
    monkeypatch.setattr(settings, "ocr_backend", "ollama_vision")
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    monkeypatch.setattr(
        "src.services.receipt_segmenter.segment_receipts",
        lambda *_a, **_k: [Image.new("RGB", (100, 100))],
    )
    monkeypatch.setattr(llm, "_image_to_ocr_bytes", lambda *_a, **_k: b"pngbytes")
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"vendor_name":"Solo","total_amount":"5","currency":"USD","date":"2026-05-01"}'
        ),
    )
    items = llm.process_with_segments("/tmp/x.jpg")
    assert len(items) == 1
    assert items[0]["crop"] is None


def test_process_normalises_each_receipt_independently(monkeypatch):
    """Locale / country / amount cleanup must run per receipt — a CAD-on-$ row
    next to a BRL-on-R$ row should each end up with the right currency."""
    monkeypatch.setattr(llm, "_pick_model", lambda: "glm-ocr:latest")
    _stub_segmentation_to_single_image(monkeypatch)
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_a, **_k: _stub_ollama_response(
            '{"receipts":['
            '{"vendor_name":"Vancouver Cafe","total_amount":"12.50","currency":"USD","date":"2026-05-01","country":"Canada","category":"meals"},'
            '{"vendor_name":"Padaria","total_amount":"41,78","currency":"BRL","date":"30/04/2026","country":"BR","language":"pt","category":"meals"}'
            "]}"
        ),
    )
    results = llm.process_receipt_image("/tmp/whatever.jpg")
    assert len(results) == 2
    # First: USD on a Canadian receipt → swapped to CAD
    assert results[0]["currency"] == "CAD"
    assert results[0]["country"] == "CA"
    # Second: BRL with comma-decimal parsed correctly
    assert results[1]["currency"] == "BRL"
    assert results[1]["total_amount"] == 41.78
    assert results[1]["country"] == "BR"


# ─── _preprocess_for_ocr ────────────────────────────────────────────────────


def test_preprocess_respects_disable_flag(monkeypatch):
    """When the config knob is off, preprocess is a no-op and returns the
    same PIL image instance — no rotation, no contrast, no sharpening."""
    monkeypatch.setattr(settings, "ocr_preprocess", False)
    img = Image.new("RGB", (200, 300), color=(120, 120, 120))
    out = llm._preprocess_for_ocr(img)
    assert out is img  # identity, not a copy


def test_preprocess_returns_rgb_image_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ocr_preprocess", True)
    img = Image.new("RGB", (200, 300), color=(120, 120, 120))
    out = llm._preprocess_for_ocr(img)
    assert isinstance(out, Image.Image)
    assert out.mode == "RGB"
    assert out.size == (200, 300)  # dimensions unchanged


def test_preprocess_respects_exif_orientation(monkeypatch):
    """A JPEG with EXIF orientation=6 (rotate 90° CW) should come out of
    preprocess physically rotated — vision models can't read sideways."""
    import io

    monkeypatch.setattr(settings, "ocr_preprocess", True)
    # Build a JPEG with EXIF orientation tag = 6
    img = Image.new("RGB", (300, 200), color=(120, 120, 120))
    buf = io.BytesIO()
    # PIL doesn't write EXIF by default; use piexif-style bytes via save kwargs
    exif_bytes = (
        b"Exif\x00\x00"
        b"II*\x00"  # little-endian TIFF header
        b"\x08\x00\x00\x00"  # offset to first IFD
        b"\x01\x00"  # 1 entry
        b"\x12\x01\x03\x00\x01\x00\x00\x00\x06\x00\x00\x00"  # Orientation=6 (SHORT, count 1, value 6)
        b"\x00\x00\x00\x00"  # next IFD offset
    )
    img.save(buf, format="JPEG", exif=exif_bytes)
    buf.seek(0)
    pic = Image.open(buf)
    out = llm._preprocess_for_ocr(pic)
    # Rotate-90 swaps dimensions
    assert out.size == (200, 300)


def test_preprocess_changes_pixels_for_uneven_lighting(monkeypatch):
    """CLAHE should redistribute luminance on an image with strong gradient.
    We don't assert exact pixel values — just that the output differs from
    the input, proving the contrast op actually ran."""
    import numpy as np

    monkeypatch.setattr(settings, "ocr_preprocess", True)
    # Diagonal gradient from 50 (dark) to 200 (bright)
    w, h = 200, 200
    pixels = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            v = int(50 + (x + y) * 150 / (w + h))
            pixels[y, x] = [v, v, v]
    img = Image.fromarray(pixels)
    out = llm._preprocess_for_ocr(img)
    diff = np.abs(np.array(img).astype(int) - np.array(out).astype(int))
    # Mean per-pixel difference should be non-trivial — CLAHE redistributed
    # the histogram in a way the unsharp mask amplified.
    assert diff.mean() > 1.0


def test_preprocess_fails_open_on_unexpected_error(monkeypatch):
    """If something inside preprocess raises (e.g. cv2 issue on a weird
    image), the original image should still come out — we never break the
    OCR call because of preprocessing."""
    monkeypatch.setattr(settings, "ocr_preprocess", True)
    # Force the inner cv2 call to blow up
    import cv2

    monkeypatch.setattr(
        cv2, "createCLAHE", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    img = Image.new("RGB", (100, 100), color="white")
    out = llm._preprocess_for_ocr(img)
    assert isinstance(out, Image.Image)


def test_load_image_bytes_resizes_and_returns_bounded_png(tmp_path):
    """A 12 MP receipt on disk must come back as a bounded PNG payload.

    Core safety guarantee: regardless of source resolution, what we send to
    Ollama is small enough that the vision encoder won't thrash the machine.
    PNG (lossless) is used instead of JPEG because glm-ocr's input parser
    intermittently rejects Pillow-encoded JPEGs.
    """
    # A realistic photo-like image. Flat-colour fills would compress to a
    # few KB and not test the budget meaningfully. Smooth gradient with
    # noise is close to what a real downscaled receipt looks like.
    import random

    random.seed(0)
    pixels = []
    for y in range(3024):
        for x in range(4032):
            # Diagonal gradient + light noise
            v = (x + y) // 30 % 256
            pixels.append((v, v, v))
    huge = Image.new("RGB", (4032, 3024))
    huge.putdata(pixels)
    path = tmp_path / "big.png"
    huge.save(path, format="PNG")

    out = llm._load_image_bytes(str(path))

    # Must be a valid PNG that round-trips
    decoded = Image.open(BytesIO(out))
    assert decoded.format == "PNG"
    # Longest edge must respect the configured cap
    assert max(decoded.size) <= settings.ocr_max_image_dim
    # And the payload itself must be bounded — anything over ~4 MB defeats
    # the purpose. PNG compresses smooth tones efficiently; receipts at
    # 1536 px land at ~0.5-1.5 MB in practice.
    assert len(out) < 4 * 1024 * 1024, f"OCR payload too large: {len(out)} bytes"
