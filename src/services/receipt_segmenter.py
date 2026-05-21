"""
Detect distinct receipt regions on a single scanned/photographed page and
return one cropped PIL image per region.

The OCR pipeline calls this BEFORE handing pixels to the vision model.
For a typical single-receipt phone photo, the segmenter returns a 1-element
list (the original image) and OCR proceeds as usual. For a flat-bed scan of
3 receipts side-by-side, it returns 3 elements and the OCR pipeline runs
once per crop — far more reliable than asking a small vision model to
enumerate every receipt itself.

Assumptions:
- Receipts are LIGHTER than the background (paper on table, scanner bed, etc.)
- Receipts don't significantly overlap. Touching is fine; overlapping
  receipts will be merged into one segment.
- Receipts are at least ~3% of the total image area each. Tiny crops would
  be too low-resolution for OCR anyway.

Falls back to "return the whole image as one segment" on any failure mode
(unreadable file, cv2 import problem, weird thresholding, etc.) so the
OCR path never breaks because of segmentation.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import List

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# ─── Tunable thresholds ─────────────────────────────────────────────────────
# These are intentionally conservative. Receipts much smaller than 3% of the
# image area get merged or dropped; that's fine because the OCR would fail
# on them anyway.
MIN_AREA_FRACTION = 0.03   # ignore blobs smaller than 3% of image area
MAX_AREA_FRACTION = 0.85   # ignore blobs bigger than 85% (likely the page)
MIN_FILL_FRACTION = 0.25   # contour-area / bbox-area; rejects sparse blobs
PADDING_PX = 24             # bbox padding so we don't clip receipt edges
MORPH_KERNEL_PX = 21        # kernel for the close op (fill gaps within receipts)

# ─── Projection-based grid detection (primary method) ──────────────────────
# A "foreground row" is any row whose density of foreground pixels exceeds
# PROJ_DENSITY_FRAC of the row width. Same idea per column.
PROJ_DENSITY_FRAC = 0.05    # >5% of row pixels white = receipt present here
# A run of foreground rows/cols counts as a receipt span only if it occupies
# at least PROJ_MIN_SPAN_FRAC of the image dim. Drops thin noise lines.
PROJ_MIN_SPAN_FRAC = 0.05
# Gaps between receipts must be at least PROJ_MIN_GAP_FRAC of the dim, or
# we merge adjacent runs (handles dotted edges, small light bleed).
PROJ_MIN_GAP_FRAC = 0.015


def segment_receipts(image_path: str) -> List[Image.Image]:
    """
    Return one PIL image per detected receipt region. The list always has at
    least one element — if segmentation can't find ≥2 plausible regions,
    we return `[whole_image]` so the OCR pipeline still runs.
    """
    try:
        return _segment_impl(image_path)
    except Exception as e:
        logger.warning("Segmentation failed for %s: %s — using whole image", image_path, e)
        return [_open_as_rgb(image_path)]


def segment_with_boxes(image_path: str) -> tuple:
    """
    Same detection as segment_receipts, but returns bounding boxes (in image
    pixel coordinates) plus the original image's (w, h). Used by the
    bbox-editor UI so the user can review / adjust the auto-detected regions
    before OCR runs.

    Returns: (boxes: list[{"x":int,"y":int,"w":int,"h":int}], (width, height))

    On segmentation failure or if no ≥2 regions are found, returns a single
    box covering the whole image (so the editor still has something to show).
    """
    pil = _open_as_rgb(image_path)
    w, h = pil.size
    try:
        boxes = _compute_boxes(image_path, pil)
    except Exception as e:
        logger.warning("segment_with_boxes failed for %s: %s — falling back", image_path, e)
        boxes = []
    if not boxes:
        boxes = [{"x": 0, "y": 0, "w": w, "h": h}]
    return boxes, (w, h)


def _compute_boxes(image_path: str, pil: Image.Image) -> list:
    """Detect receipt bboxes. Tries strategies in this order:

      1. Text-cluster (Apple Vision OCR + spatial union-find on text
         bounding boxes). Best for photos where receipts share the
         background brightness — adjacent receipts produce distinct
         clusters even with thin gaps because we cluster spatially
         distinct text regions, not pixel continuity.
      2. Projection-profile grid (row-then-column density slicing).
         Works without Apple Vision; good for clear grids on dark
         backgrounds.
      3. Contour analysis on a thresholded mask (the original method).

    Returns a list of {"x","y","w","h"} dicts in image pixel coordinates,
    sorted in reading order (top-to-bottom, then left-to-right).
    """
    import cv2

    arr = np.array(pil)
    h, w = arr.shape[:2]
    img_area = h * w
    if img_area == 0:
        return []

    # 0) Text-cluster method (Apple Vision)
    try:
        text_boxes = _text_cluster_boxes(image_path, (w, h))
    except Exception as e:
        logger.debug("text-cluster segmentation skipped: %s", e)
        text_boxes = []
    if len(text_boxes) >= 2:
        text_boxes = [_pad_box((b["x"], b["y"], b["w"], b["h"]), w, h) for b in text_boxes]
        text_boxes.sort(key=lambda b: (b["y"] // max(50, h // 20), b["x"]))
        logger.info("segmenter (boxes): text-cluster found %d region(s)", len(text_boxes))
        return text_boxes

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)

    # 1) Projection-profile grid detection — robust for grid layouts where
    # contour analysis incorrectly merges or splits adjacent receipts.
    for label, threshold_fn in _threshold_strategies(blurred):
        mask = _foreground_mask(threshold_fn(), min_dim=min(h, w))
        spans = _grid_spans(mask)
        if len(spans) >= 2:
            boxes = [_pad_box(s, w, h) for s in spans]
            boxes.sort(key=lambda b: (b["y"] // max(50, h // 20), b["x"]))
            logger.info(
                "segmenter (boxes): projection strategy=%s found %d region(s)",
                label, len(boxes),
            )
            return boxes

    # 2) Fall back to the contour-based method for cases where the
    # projection split doesn't produce a clean grid (e.g. a single
    # crooked receipt or strong shadows).
    for label, threshold_fn in _threshold_strategies(blurred):
        thresh = threshold_fn()
        k = max(3, MORPH_KERNEL_PX if min(h, w) > 200 else 5)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < img_area * MIN_AREA_FRACTION or area > img_area * MAX_AREA_FRACTION:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            bbox_area = float(bw * bh)
            if bbox_area == 0 or (area / bbox_area) < MIN_FILL_FRACTION:
                continue
            candidates.append(_pad_box((x, y, bw, bh), w, h))

        if len(candidates) >= 2:
            logger.info("segmenter (boxes): contour strategy=%s found %d region(s)", label, len(candidates))
            row_h = max(50, h // 20)
            candidates.sort(key=lambda b: (b["y"] // row_h, b["x"]))
            return candidates

    return []


def _foreground_mask(thresh, min_dim: int):
    """Apply a moderate morphological close so a single receipt's interior
    forms one connected component, without bridging to neighbours."""
    import cv2
    # Smaller kernel than the contour pass so adjacent receipts stay split
    k = max(3, min_dim // 200) | 1   # keep odd
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    return cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)


def _runs(mask_1d: np.ndarray, min_len: int) -> list:
    """Find runs of True values longer than min_len. Returns [(start, end)]."""
    spans = []
    in_run = False
    start = 0
    for i, v in enumerate(mask_1d):
        if v and not in_run:
            in_run, start = True, i
        elif not v and in_run:
            in_run = False
            if i - start >= min_len:
                spans.append((start, i))
    if in_run and len(mask_1d) - start >= min_len:
        spans.append((start, len(mask_1d)))
    return spans


def _merge_close_spans(spans: list, min_gap: int) -> list:
    """Merge spans separated by less than min_gap (handles dotted receipt
    edges where the projection briefly dips below threshold)."""
    if not spans:
        return spans
    out = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = out[-1]
        if s - pe < min_gap:
            out[-1] = (ps, e)
        else:
            out.append((s, e))
    return out


def _grid_spans(mask: np.ndarray) -> list:
    """Decompose a foreground mask into receipt bboxes via row-then-column
    projection profiles. Returns a list of (x, y, w, h) tuples.

    Algorithm:
      1. Sum foreground pixels per row → find rows with enough density.
      2. Group those rows into row-bands (one row-band = one row of receipts).
      3. Within each row-band, sum per column → find column-bands
         (one column-band = one receipt in that row).
      4. Emit a bbox per (row-band × column-band) cell.

    This handles the common "receipts arranged in a grid on a table" case
    much better than connected-component analysis — receipts that touch
    or share a shadow no longer merge into one blob, because we look at
    overall density patterns rather than continuity of pixels.
    """
    H, W = mask.shape
    bin_mask = (mask > 0).astype(np.uint8)

    row_sum = bin_mask.sum(axis=1)
    row_active = row_sum > (PROJ_DENSITY_FRAC * W)
    row_spans = _merge_close_spans(
        _runs(row_active, int(PROJ_MIN_SPAN_FRAC * H)),
        int(PROJ_MIN_GAP_FRAC * H),
    )
    if not row_spans:
        return []

    cells: list = []
    for ry0, ry1 in row_spans:
        band = bin_mask[ry0:ry1]
        col_sum = band.sum(axis=0)
        col_active = col_sum > (PROJ_DENSITY_FRAC * (ry1 - ry0))
        col_spans = _merge_close_spans(
            _runs(col_active, int(PROJ_MIN_SPAN_FRAC * W)),
            int(PROJ_MIN_GAP_FRAC * W),
        )
        if not col_spans:
            continue
        for cx0, cx1 in col_spans:
            # Tighten Y to the actual foreground inside this cell — the
            # row-band may be padded by a tall neighbour in the same row.
            cell = bin_mask[ry0:ry1, cx0:cx1]
            tight_y = _tighten(cell.sum(axis=1) > 0)
            if tight_y is None:
                continue
            ty0, ty1 = tight_y
            cells.append((cx0, ry0 + ty0, cx1 - cx0, ty1 - ty0))
    return cells


def _tighten(mask_1d: np.ndarray) -> tuple:
    """Return (first_true_idx, last_true_idx+1) or None if no True values."""
    where = np.flatnonzero(mask_1d)
    if len(where) == 0:
        return None
    return int(where[0]), int(where[-1]) + 1


def _pad_box(box, img_w: int, img_h: int) -> dict:
    """Add PADDING_PX of slack around a (x,y,w,h) tuple, clamped to image."""
    x, y, bw, bh = box
    x0 = max(0, x - PADDING_PX)
    y0 = max(0, y - PADDING_PX)
    x1 = min(img_w, x + bw + PADDING_PX)
    y1 = min(img_h, y + bh + PADDING_PX)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# Text-cluster method tunables — only used when Apple Vision is available.
CLUSTER_EPS_FRAC = 0.025      # gap < 2.5% of max(W,H) → same receipt
CLUSTER_MIN_BOXES = 3         # drop singletons / noise
CLUSTER_MERGE_GAP_FRAC = 0.03 # post-pass: merge cluster bboxes whose gap < this


def _bbox_gap(a: tuple, b: tuple) -> float:
    """Euclidean min-distance between two (x,y,w,h) rectangles. 0 if touch."""
    ax0, ay0, aw, ah = a
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx0, by0, bw, bh = b
    bx1, by1 = bx0 + bw, by0 + bh
    dx = max(0.0, max(bx0 - ax1, ax0 - bx1))
    dy = max(0.0, max(by0 - ay1, ay0 - by1))
    return (dx * dx + dy * dy) ** 0.5


def _union_find_cluster(text_bboxes: list, eps: float) -> list:
    """Union-find: each text bbox is a node, edge if gap < eps. Returns
    a list of cluster lists (each cluster = list of text bboxes)."""
    n = len(text_bboxes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if _bbox_gap(text_bboxes[i], text_bboxes[j]) < eps:
                union(i, j)

    clusters: dict = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(text_bboxes[i])
    return list(clusters.values())


def _cluster_bbox(cluster: list) -> tuple:
    """Bounding box of a list of (x,y,w,h) rects → (x,y,w,h)."""
    xs = [b[0] for b in cluster] + [b[0] + b[2] for b in cluster]
    ys = [b[1] for b in cluster] + [b[1] + b[3] for b in cluster]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (x0, y0, x1 - x0, y1 - y0)


def _merge_close_cluster_boxes(boxes: list, gap: float) -> list:
    """Second-pass union-find on already-clustered bounding rects to merge
    receipts that were split (e.g. a printed logo block far from the body).
    """
    if len(boxes) <= 1:
        return boxes
    n = len(boxes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _bbox_gap(boxes[i], boxes[j]) < gap:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(boxes[i])
    return [_cluster_bbox(g) for g in groups.values()]


def _text_cluster_boxes(image_path: str, image_size: tuple) -> list:
    """Use Apple Vision text bboxes + spatial clustering to find receipt
    regions. Returns [{"x","y","w","h"}] dicts (no padding — caller adds).

    Empty list if Apple Vision is unavailable, returns < 2 useful clusters,
    or any error occurs (caller falls back to image-only methods).
    """
    from . import apple_vision_ocr
    if not apple_vision_ocr.is_available():
        return []

    W, H = image_size
    text_items = apple_vision_ocr.extract_text_boxes_from_file(image_path, image_size)
    if len(text_items) < 2:
        return []

    text_bboxes = [it["bbox"] for it in text_items]
    eps = max(W, H) * CLUSTER_EPS_FRAC
    clusters = _union_find_cluster(text_bboxes, eps)
    # Drop singletons / very small clusters that are usually OCR noise
    sized = [c for c in clusters if len(c) >= CLUSTER_MIN_BOXES]
    if len(sized) < 2:
        return []

    # First-pass cluster bboxes
    cluster_rects = [_cluster_bbox(c) for c in sized]
    # Merge cluster bboxes that are spatially close (handles a receipt
    # whose printed logo block clusters separately from its text body).
    merge_gap = max(W, H) * CLUSTER_MERGE_GAP_FRAC
    cluster_rects = _merge_close_cluster_boxes(cluster_rects, merge_gap)

    return [{"x": x, "y": y, "w": w, "h": h} for (x, y, w, h) in cluster_rects]


def _open_as_rgb(image_path: str) -> Image.Image:
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _threshold_strategies(blurred):
    """Yield (label, threshold-fn) pairs to try in order. Each fn must return
    a binary mask where receipts are white (255) and background is black (0)."""
    import cv2

    def _otsu():
        _, m = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return m

    def _fixed(value):
        def fn():
            _, m = cv2.threshold(blurred, value, 255, cv2.THRESH_BINARY)
            return m
        return fn

    return [
        ("otsu",     _otsu),
        ("fixed210", _fixed(210)),
        ("fixed200", _fixed(200)),
        ("fixed190", _fixed(190)),
    ]


def _segment_impl(image_path: str) -> List[Image.Image]:
    """Crop variant — reuses `_compute_boxes` so the cropping path and the
    bbox-editor path can never disagree on what the segmenter found."""
    pil = _open_as_rgb(image_path)
    arr = np.array(pil)
    boxes = _compute_boxes(image_path, pil)
    if len(boxes) < 2:
        return [pil]
    crops: List[Image.Image] = []
    for b in boxes:
        crop_arr = arr[b["y"]: b["y"] + b["h"], b["x"]: b["x"] + b["w"]]
        crops.append(Image.fromarray(crop_arr))
    logger.info("segment_receipts: %d region(s) in %s", len(crops), Path(image_path).name)
    return crops
