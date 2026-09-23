"""TypeSafe-assisted segmentation — the HTTP seam (`typesafe_categorizer._post`)
is stubbed, so nothing touches the network. Covers payload shape, the merge
policy (confident same → merge across the gap rule, confident different →
split under it, low confidence → gap rule, a confident "different" walls
off transitive merges), error fallback, and that the default
`segment_backend=local` never issues a call."""

import pytest

from src.config import settings
from src.services import receipt_segmenter as rs
from src.services import typesafe_categorizer as tsc
from src.services import typesafe_segmenter as tsg

W = H = 1000  # eps = 40 px, merge gap = 50 px, pair gap = 200 px


def _cluster(x: int, y: int, lines: list[str]) -> list[dict]:
    """Stacked text boxes 20 px apart (gap 8 < eps) → one first-pass cluster."""
    return [
        {"text": t, "bbox": (x, y + 20 * k, 200, 12), "confidence": 0.9}
        for k, t in enumerate(lines)
    ]


SHOP = ["PADARIA X", "CNPJ 1", "PAO 5,00", "CAFE 4,00", "VALOR TOTAL 9,00"]
FOOTER = ["Tributos Totais IBPT", "R$ 0,90", "Fonte IBPT"]
OTHER = ["FARMACIA Y", "CNPJ 2", "DIPIRONA 8,00", "TOTAL 8,00", "CARTAO"]


def _fake_post(verdict):
    """Build a `_post` stub that answers each pair from its text via
    `verdict(a_lines, b_lines) -> (choice, confidence)`."""

    def post(payload):
        answers = {}
        for qid, _q in payload["questions"].items():
            i = int(qid[1:])
            pair = payload["state"]["pairs"][i]
            choice, conf = verdict(pair["a"]["lines"], pair["b"]["lines"])
            answers[qid] = {"type": "choice", "choice": choice, "confidence": conf}
        return {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 2}}

    return post


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "segment_backend", "typesafe")
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "typesafe_min_confidence", 0.7)


def _fake_extractor(monkeypatch, items):
    class Mod:
        @staticmethod
        def extract_text_boxes_from_file(path, size):
            return items

    monkeypatch.setattr(rs, "_text_box_extractor", lambda: Mod)


# ─── Payload ────────────────────────────────────────────────────────────────


def test_payload_trims_lines_and_uses_pair_criteria():
    long = [f"line {i}" for i in range(30)]
    p = tsg.build_payload(
        [{"a_lines": long, "b_lines": ["x"], "b_relative_to_a": "below", "gap_frac": 0.086}]
    )
    a = p["state"]["pairs"][0]["a"]["lines"]
    assert len(a) == tsg.MAX_LINES_PER_SIDE + 1 and a[tsg.MAX_LINES_PER_SIDE // 2] == "…"
    assert a[0] == "line 0" and a[-1] == "line 29"
    assert "below" in p["state"]["pairs"][0]["layout"] and "9%" in p["state"]["pairs"][0]["layout"]
    q = p["questions"]["q0"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"same_receipt", "different_receipts"}
    assert "`pairs[0].a.lines`" in q["instructions"]
    assert p["model"] == settings.typesafe_model


# ─── Merge policy ───────────────────────────────────────────────────────────


def test_confident_same_merges_footer_beyond_gap_rule(enabled, monkeypatch):
    # footer 120 px below the shop (> merge gap 50): geometry keeps it apart
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER) + _cluster(600, 0, OTHER)
    monkeypatch.setattr(tsc, "_post", _fake_post(lambda a, b: ("same_receipt", 0.95)))
    out = tsg.judged_cluster_rects(items, W, H)
    assert len(out["rects"]) == 2
    tall = max(out["rects"], key=lambda r: r[3])
    assert tall[1] == 0 and tall[1] + tall[3] == 212 + 40 + 12  # shop top → footer bottom
    geo = tsg.judged_cluster_rects(items, W, H, call_typesafe=False)
    assert len(geo["rects"]) == 3


def test_confident_different_splits_under_gap_rule(enabled, monkeypatch):
    # 45 px gap: over eps (40) so two first-pass clusters, under merge gap (50)
    items = _cluster(0, 0, SHOP) + _cluster(0, 92 + 45, OTHER)
    monkeypatch.setattr(tsc, "_post", _fake_post(lambda a, b: ("different_receipts", 0.99)))
    assert len(tsg.judged_cluster_rects(items, W, H)["rects"]) == 2
    assert len(tsg.judged_cluster_rects(items, W, H, call_typesafe=False)["rects"]) == 1


def test_low_confidence_keeps_gap_rule(enabled, monkeypatch):
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER) + _cluster(600, 0, OTHER)
    monkeypatch.setattr(tsc, "_post", _fake_post(lambda a, b: ("same_receipt", 0.5)))
    out = tsg.judged_cluster_rects(items, W, H)
    assert len(out["rects"]) == 3
    assert all(d["final"] == d["geometric"] for d in out["decisions"])


def test_confident_different_walls_off_transitive_merge(enabled, monkeypatch):
    # shop / footer / other stacked; Jev ties the footer to BOTH neighbours
    # but says shop≠other at 0.99 → footer joins the stronger tie only
    items = _cluster(0, 0, SHOP) + _cluster(0, 150, FOOTER) + _cluster(0, 260, OTHER)

    def verdict(a, b):
        if a[0] == "PADARIA X" and b[0] == "FARMACIA Y":
            return "different_receipts", 0.99
        if a[0] == "PADARIA X":
            return "same_receipt", 0.9
        return "same_receipt", 0.8  # footer–other

    monkeypatch.setattr(tsc, "_post", _fake_post(verdict))
    out = tsg.judged_cluster_rects(items, W, H)
    rects = sorted(out["rects"], key=lambda r: r[1])
    assert len(rects) == 2
    assert rects[0][1] == 0 and rects[0][1] + rects[0][3] == 150 + 40 + 12  # shop+footer
    assert rects[1][1] == 260
    assert any(d.get("blocked") for d in out["decisions"])


# ─── Segmenter integration ─────────────────────────────────────────────────


def test_default_backend_never_calls_api(monkeypatch):
    monkeypatch.setattr(settings, "segment_backend", "local")
    monkeypatch.setattr(tsc, "_post", lambda p: pytest.fail("API called with local backend"))
    assert tsg.is_enabled() is False
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER) + _cluster(600, 0, OTHER)
    _fake_extractor(monkeypatch, items)
    boxes = rs._text_cluster_boxes("x.png", (W, H))
    assert len(boxes) == 3


def test_segmenter_uses_jev_when_enabled(enabled, monkeypatch):
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER) + _cluster(600, 0, OTHER)
    _fake_extractor(monkeypatch, items)
    monkeypatch.setattr(tsc, "_post", _fake_post(lambda a, b: ("same_receipt", 0.95)))
    boxes = rs._text_cluster_boxes("x.png", (W, H))
    assert len(boxes) == 2


def test_segmenter_falls_back_to_gap_rule_on_error(enabled, monkeypatch):
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER) + _cluster(600, 0, OTHER)
    _fake_extractor(monkeypatch, items)

    def boom(p):
        raise tsg.TypeSafeError("HTTP 429")

    monkeypatch.setattr(tsc, "_post", boom)
    assert len(rs._text_cluster_boxes("x.png", (W, H))) == 3


def test_segmenter_falls_through_when_jev_leaves_one_region(enabled, monkeypatch):
    items = _cluster(0, 0, SHOP) + _cluster(0, 212, FOOTER)
    _fake_extractor(monkeypatch, items)
    monkeypatch.setattr(tsc, "_post", _fake_post(lambda a, b: ("same_receipt", 0.95)))
    assert rs._text_cluster_boxes("x.png", (W, H)) == []  # → image-only methods
