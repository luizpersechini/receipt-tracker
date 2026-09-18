"""TypeSafe categorization — client stubbed at the `_post` seam, so nothing
here touches the network. Covers payload shape, batching, result mapping,
the confidence-floor policy, error fallback, and — most importantly — that
the default `category_backend=keyword` never issues a call."""

import pytest

from src.config import settings
from src.services import typesafe_categorizer as ts
from src.services.statement_categories import VALID_CATEGORIES


def _fake_answer(cat, conf=0.95):
    probs = {c: 0.0 for c in VALID_CATEGORIES}
    probs[cat] = conf
    return {"type": "choice", "choice": cat, "confidence": conf, "probabilities": probs}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "category_backend", "typesafe")
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "typesafe_min_confidence", 0.7)


# ─── Payload shape ──────────────────────────────────────────────────────────


def test_payload_one_choice_question_per_item_with_path_refs(enabled, monkeypatch):
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"answers": {f"q{i}": _fake_answer("meals") for i in range(2)}, "usage": {}}

    monkeypatch.setattr(ts, "_post", fake_post)
    ts.categorize(
        [
            {
                "id": 7,
                "merchant_or_vendor": "PIZZA HUT",
                "amount": 46.8,
                "currency": "BRL",
                "date": "2026-07-23",
            },
            {
                "id": 8,
                "merchant_or_vendor": "UBER",
                "amount": 18.97,
                "currency": "BRL",
                "date": "2026-07-23",
                "items": [{"description": "Trip fare"}],
            },
        ]
    )
    p = captured["payload"]
    assert p["model"] == settings.typesafe_model
    assert len(p["state"]["items"]) == 2
    assert p["state"]["items"][0]["merchant_or_vendor"] == "PIZZA HUT"
    # one choice question per item, criteria == exactly VALID_CATEGORIES
    assert set(p["questions"]) == {"q0", "q1"}
    for qid, q in p["questions"].items():
        assert q["type"] == "choice"
        assert set(q["criteria"]) == VALID_CATEGORIES
        assert all(isinstance(v, str) and v for v in q["criteria"].values())
    # instructions reference the item by path
    assert "`items[0].merchant_or_vendor`" in p["questions"]["q0"]["instructions"]
    assert "`items[1].items`" in p["questions"]["q1"]["instructions"]  # has line items
    assert "`items[0].items`" not in p["questions"]["q0"]["instructions"]  # none
    # no internal ids leak into state
    assert "id" not in p["state"]["items"][0]


# ─── Results + batching ─────────────────────────────────────────────────────


def test_results_map_back_by_id_in_order(enabled, monkeypatch):
    monkeypatch.setattr(
        ts,
        "_post",
        lambda payload: {
            "answers": {"q0": _fake_answer("lodging", 0.9), "q1": _fake_answer("fuel", 0.6)},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    )
    out = ts.categorize(
        [{"id": "a", "merchant_or_vendor": "HOTEL"}, {"id": "b", "merchant_or_vendor": "POSTO"}]
    )
    assert [(r["id"], r["category"], r["confidence"]) for r in out] == [
        ("a", "lodging", 0.9),
        ("b", "fuel", 0.6),
    ]
    assert out[0]["probabilities"]["lodging"] == 0.9


def test_chunks_into_batches_of_50(enabled, monkeypatch):
    calls = []

    def fake_post(payload):
        n = len(payload["state"]["items"])
        calls.append(n)
        return {"answers": {f"q{i}": _fake_answer("other") for i in range(n)}, "usage": {}}

    monkeypatch.setattr(ts, "_post", fake_post)
    out = ts.categorize([{"id": i, "merchant_or_vendor": f"M{i}"} for i in range(120)])
    assert calls == [50, 50, 20]
    assert len(out) == 120 and [r["id"] for r in out] == list(range(120))


def test_unexpected_category_is_untrusted_other(enabled, monkeypatch):
    monkeypatch.setattr(
        ts,
        "_post",
        lambda p: {"answers": {"q0": {"choice": "groceries", "confidence": 0.99}}, "usage": {}},
    )
    (r,) = ts.categorize([{"id": 1, "merchant_or_vendor": "X"}])
    assert r["category"] == "other" and r["confidence"] == 0.0


# ─── Confidence policy ──────────────────────────────────────────────────────


def test_pick_or_fallback_threshold(enabled):
    assert (
        ts.pick_or_fallback({"id": 1, "category": "meals", "confidence": 0.7}, "other") == "meals"
    )
    assert (
        ts.pick_or_fallback({"id": 1, "category": "meals", "confidence": 0.69}, "other") == "other"
    )
    assert ts.pick_or_fallback(None, "travel") == "travel"


# ─── Errors → fallback, never network in tests ──────────────────────────────


def test_http_error_raises_typesafe_error(enabled, monkeypatch):
    class R:
        status_code = 429
        text = "slow down"

    monkeypatch.setattr(ts.requests, "post", lambda *a, **k: R())
    with pytest.raises(ts.TypeSafeError):
        ts.categorize([{"id": 1, "merchant_or_vendor": "X"}])


def test_missing_key_raises_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "category_backend", "typesafe")
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(ts.requests, "post", lambda *a, **k: pytest.fail("must not reach network"))
    with pytest.raises(ts.TypeSafeError, match="TYPESAFE_API_KEY"):
        ts.categorize([{"id": 1, "merchant_or_vendor": "X"}])


# ─── Default behaviour unchanged: keyword backend makes NO call ─────────────


def test_default_backend_never_calls_api(monkeypatch):
    monkeypatch.setattr(settings, "category_backend", "keyword")
    monkeypatch.setattr(ts, "_post", lambda p: pytest.fail("API called with keyword backend"))
    assert ts.is_enabled() is False

    # statement parser post-pass is a no-op
    from src.services import statement_pdf_parser as spp

    txns = [{"description": "PIZZA HUT", "amount": 46.8, "category": "meals"}]
    spp._apply_typesafe_categories(txns)
    assert txns[0]["category"] == "meals"

    # OCR fallback returns the keyword guess untouched
    from src.services import llm_processor as lp

    assert lp._category_with_typesafe({"vendor_name": "POSTO"}, "fuel") == "fuel"


def test_statement_postpass_uses_typesafe_above_floor_only(enabled, monkeypatch):
    from src.services import statement_pdf_parser as spp

    monkeypatch.setattr(
        ts,
        "_post",
        lambda p: {
            "answers": {"q0": _fake_answer("lodging", 0.95), "q1": _fake_answer("fuel", 0.4)},
            "usage": {},
        },
    )
    txns = [
        {"description": "HOTEL X", "amount": 300.0, "category": "other"},
        {"description": "POSTO Y", "amount": 200.0, "category": "transportation"},
    ]
    spp._apply_typesafe_categories(txns)
    assert txns[0]["category"] == "lodging"  # confident → used
    assert txns[1]["category"] == "transportation"  # 0.4 < 0.7 → keyword kept
