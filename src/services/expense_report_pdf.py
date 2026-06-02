"""Build the combined expense-report PDF from a reconciled statement.

Output mirrors the Atlantic Nickel "Prestação de Contas - Cartão
Corporativo" form the user provided:

  Page 1     — front-page form: header + employee band + categorised
               expense table + IOF row + grand total + bank/signature
               footer. Unmatched statement transactions render with a
               "sem comprovante — justificativa" note so the reviewer can
               hand-write a reason.
  Page 2..N  — every matched receipt as an image, packed into a 3-column
               grid (break-inside: avoid keeps a receipt+caption together),
               captioned with date · vendor · amount. Uber/Gmail rows with
               no image file get a rendered synthetic card instead.

Rendering is HTML/CSS → WeasyPrint. Images are embedded as base64 data
URIs (downscaled) so there's no external file dependency and the PDF is
self-contained.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from html import escape
from typing import Any, Optional

from ..config import settings
from ..db import database as db
from . import report_profile, report_users

logger = logging.getLogger(__name__)


# ─── Category bucketing ─────────────────────────────────────────────────────
# Maps a receipt/transaction category (+ free-text label) onto one of the
# five form columns.
_COLS = ["combustivel", "alimentacao", "taxi", "material", "outros"]
_COL_LABELS = {
    "combustivel": "Combustível,\nEstacionamento",
    "alimentacao": "Despesas com\nalimentação",
    "taxi": "Taxi /\nCorreios",
    "material": "Material para\nescritório em geral",
    "outros": "Outros",
}

_FUEL_PARKING_KEYWORDS = (
    "parking",
    "estacion",
    "posto",
    "fuel",
    "combust",
    "gas station",
    "shell",
    "petrobras",
    "ipiranga",
    "parkchamp",
    "garagem",
)


def _bucket(category: Optional[str], label: Optional[str]) -> str:
    cat = (category or "").lower()
    text = (label or "").lower()
    if any(k in text for k in _FUEL_PARKING_KEYWORDS):
        return "combustivel"
    if cat == "meals":
        return "alimentacao"
    if cat == "transportation":
        return "taxi"
    if cat == "office_supplies":
        return "material"
    # travel, lodging, other, unknown → Outros
    return "outros"


# ─── Money / date formatting (pt-BR) ────────────────────────────────────────


def _fmt_brl(v: Optional[float]) -> str:
    if v is None:
        return ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    s = f"{n:,.2f}"  # 1,234.56
    # Swap to pt-BR: 1.234,56
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _fmt_date_br(iso: Optional[str]) -> str:
    if not iso:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return str(iso)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"


# ─── IOF extraction ─────────────────────────────────────────────────────────

_IOF_RE = re.compile(r"Repasse de IOF.*?R\$\s*([\d.,]+)", re.IGNORECASE | re.DOTALL)


def _extract_iof(statement: dict) -> Optional[float]:
    """Pull the 'Repasse de IOF em R$ X' total from the saved statement PDF.

    The parser intentionally drops IOF from the transaction list (it's a tax,
    not a merchant charge), but the front-page form shows it as a line. Read
    it lazily from the file so we don't have to re-parse stored statements.
    """
    fp = statement.get("filepath")
    if not fp or not str(fp).lower().endswith(".pdf"):
        return None
    try:
        import fitz

        doc = fitz.open(fp)
        try:
            text = "\n".join(p.get_text() for p in doc)
        finally:
            doc.close()
    except Exception as e:  # pragma: no cover — file/fitz issues
        logger.warning("IOF extraction failed for %s: %s", fp, e)
        return None
    m = _IOF_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip().replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


# ─── Receipt image normalisation ────────────────────────────────────────────


def _image_data_uri(filepath: Optional[str], *, max_edge: int = 1000) -> Optional[str]:
    """Return a base64 data URI for a receipt file, downscaled to keep the
    PDF small. Handles images, HEIC/HEIF, and PDFs (first page). Returns
    None when there's no usable on-disk file (caller renders a text card)."""
    if not filepath:
        return None
    fp = str(filepath)
    if fp.startswith("gmail://"):
        # Gmail-sourced rows may have a rendered PDF on disk elsewhere; if the
        # path isn't a real file we fall back to a synthetic card.
        return None
    import os

    if not os.path.exists(fp):
        return None

    try:
        from PIL import Image

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass

        if fp.lower().endswith(".pdf"):
            import fitz

            doc = fitz.open(fp)
            try:
                page = doc[0]
                zoom = max_edge / max(page.rect.width, page.rect.height)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                img = Image.open(io.BytesIO(pix.tobytes("png")))
            finally:
                doc.close()
        else:
            img = Image.open(fp)

        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.warning("Receipt image normalise failed for %s: %s", fp, e)
        return None


def _logo_data_uri() -> Optional[str]:
    path = settings.data_dir.parent / "static" / "report_assets" / "logo.png"
    try:
        import os

        # When run from the project root, static/ is a sibling of data/'s
        # parent... resolve robustly: try a couple of candidate locations.
        candidates = [
            path,
            __import__("pathlib").Path("static/report_assets/logo.png"),
        ]
        for c in candidates:
            if os.path.exists(c):
                b64 = base64.b64encode(open(c, "rb").read()).decode("ascii")
                return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.warning("logo load failed: %s", e)
    return None


# ─── Data assembly ──────────────────────────────────────────────────────────


def _receipt_meta(r: Optional[dict]) -> dict:
    if not r:
        return {}
    try:
        return json.loads(r.get("metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _assemble(statement_id: int) -> dict[str, Any]:
    """Gather everything the templates need into one dict."""
    statement = db.get_statement_by_id(statement_id)
    if not statement:
        raise ValueError(f"Statement {statement_id} not found")

    matches = db.get_matches_for_statement(statement_id)
    # transaction_index → [match rows]
    by_idx: dict[int, list] = {}
    for m in matches:
        idx = m.get("transaction_index")
        if idx is not None:
            by_idx.setdefault(idx, []).append(m)

    transactions = statement.get("transactions") or []
    rows: list[dict] = []
    receipts_for_grid: list[dict] = []

    for i, t in enumerate(transactions):
        ms = sorted(by_idx.get(i, []), key=lambda m: m.get("id") or 0)
        amount = float(t.get("amount") or 0)
        label = t.get("description") or ""
        # Category: prefer the matched receipt's category, else the txn's.
        category = t.get("category")
        if ms:
            rmeta0 = _receipt_meta_from_match(ms[0])
            category = rmeta0.get("category") or category
        col = _bucket(category, label)

        row = {
            "date": _fmt_date_br(t.get("date")),
            "conta": "",  # filled from profile below
            "detalhe": label.upper().strip() or "—",
            "col": col,
            "amount": amount,
            "matched": bool(ms),
        }
        rows.append(row)

        # Collect receipt images for the grid (one card per matched receipt).
        for m in ms:
            rid = m.get("receipt_id")
            receipt = db.get_receipt_by_id(rid) if rid is not None else None
            rmeta = _receipt_meta(receipt)
            data_uri = _image_data_uri(receipt.get("filepath") if receipt else None)
            receipts_for_grid.append(
                {
                    "vendor": rmeta.get("vendor_name") or label or "Receipt",
                    "date": _fmt_date_br(rmeta.get("date") or t.get("date")),
                    "amount": rmeta.get("total_amount"),
                    "currency": rmeta.get("currency") or "BRL",
                    "img": data_uri,
                }
            )

    iof = _extract_iof(statement)

    return {
        "statement": statement,
        "rows": rows,
        "iof": iof,
        "receipts": receipts_for_grid,
    }


def _receipt_meta_from_match(m: dict) -> dict:
    try:
        return json.loads(m.get("receipt_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


# ─── HTML rendering ─────────────────────────────────────────────────────────


def _front_page_html(data: dict, profile: dict) -> str:
    rows = data["rows"]
    iof = data["iof"]

    # Column totals + grand total.
    col_totals = {c: 0.0 for c in _COLS}
    for r in rows:
        col_totals[r["col"]] += r["amount"]
    if iof:
        col_totals["outros"] += iof
    grand_total = sum(col_totals.values())

    conta = escape(profile.get("conta_contabil") or "")
    projeto = escape(profile.get("projeto") or "")

    def _amount_cells(col: str, amount: float) -> str:
        cells = []
        for c in _COLS:
            cells.append(f'<td class="num">{_fmt_brl(amount) if c == col else ""}</td>')
        return "".join(cells)

    body_rows = []
    for r in rows:
        detalhe = escape(r["detalhe"])
        if not r["matched"]:
            detalhe += (
                ' <span class="noreceipt">(sem comprovante — justificativa: ____________)</span>'
            )
        body_rows.append(
            f"<tr>"
            f'<td class="dt">{escape(r["date"])}</td>'
            f'<td class="ct">{conta}</td>'
            f'<td class="ds">{detalhe}</td>'
            f"{_amount_cells(r['col'], r['amount'])}"
            f'<td class="num tot">{_fmt_brl(r["amount"])}</td>'
            f'<td class="pj">{projeto}</td>'
            f"</tr>"
        )

    if iof:
        body_rows.append(
            f"<tr>"
            f'<td class="dt"></td><td class="ct"></td>'
            f'<td class="ds">IOF</td>'
            f"{_amount_cells('outros', iof)}"
            f'<td class="num tot">{_fmt_brl(iof)}</td>'
            f'<td class="pj"></td>'
            f"</tr>"
        )

    # A couple of blank rows like the printed form, then the totals row.
    for _ in range(2):
        body_rows.append(
            '<tr class="blank"><td></td><td></td><td></td>'
            + "".join("<td></td>" for _ in _COLS)
            + "<td></td><td></td></tr>"
        )

    total_row = (
        '<tr class="grand">'
        "<td></td><td></td><td></td>"
        + "".join(f'<td class="num">{_fmt_brl(col_totals[c])}</td>' for c in _COLS)
        + f'<td class="num tot">{_fmt_brl(grand_total)}</td>'
        + "<td></td></tr>"
    )

    logo = _logo_data_uri()
    logo_html = (
        f'<img class="logo" src="{logo}" alt="logo">'
        if logo
        else (f'<div class="logo-text">{escape(profile.get("company_name") or "")}</div>')
    )

    # WeasyPrint doesn't reliably paint native <input checked> boxes, so use
    # unicode glyphs — ☑ for the active kind, ☐ for the other.
    is_reembolso = profile.get("expense_kind") == "reembolso"
    reembolso_box = "☑" if is_reembolso else "☐"
    cartao_box = "☐" if is_reembolso else "☑"

    stmt_date = ""
    # Use the latest transaction date as the report date if available.
    dates = [r["date"] for r in rows if r["date"]]
    if dates:
        stmt_date = sorted(dates)[-1]

    col_headers = "".join(
        f'<th class="cat">{escape(_COL_LABELS[c]).replace(chr(10), "<br>")}</th>' for c in _COLS
    )

    return f"""
    <section class="front">
      <table class="header">
        <tr>
          <td class="hl">{logo_html}</td>
          <td class="ht">{escape(profile.get("report_title") or "")}</td>
          <td class="hc">
            <div><span>Código</span><b>{escape(profile.get("codigo") or "")}</b></div>
            <div><span>Data</span><b>{escape(stmt_date)}</b></div>
            <div><span>Página</span><b>01 / 01</b></div>
          </td>
        </tr>
      </table>

      <table class="emp">
        <tr>
          <td class="lbl r">Nome:</td><td class="val"><b>{escape(profile.get("nome") or "")}</b></td>
          <td class="lbl r">Data:</td><td class="val"><b>{escape(stmt_date)}</b></td>
          <td class="lbl r">C/C</td><td class="val"><b>{escape(profile.get("cost_center") or "")}</b></td>
        </tr>
        <tr>
          <td class="lbl r">Motivo do Gasto:</td><td class="val"><b>{escape(profile.get("motivo") or "")}</b></td>
          <td colspan="2" class="chk"><span class="cbx">{reembolso_box}</span> Reembolso despesa</td>
          <td colspan="2" class="chk"><span class="cbx">{cartao_box}</span> Cartão Crédito</td>
        </tr>
        <tr>
          <td class="lbl r">Local:</td><td class="val"><b>{escape(profile.get("local") or "")}</b></td>
          <td colspan="2"></td>
          <td class="lbl r">Moeda:</td><td class="val">{escape(profile.get("moeda") or "R$")}</td>
        </tr>
      </table>

      <table class="grid">
        <thead>
          <tr>
            <th>Data</th><th>Conta Contábil</th><th>Detalhamento da despesa</th>
            {col_headers}
            <th>Total</th><th>Projeto</th>
          </tr>
        </thead>
        <tbody>
          {"".join(body_rows)}
          {total_row}
        </tbody>
      </table>

      <table class="footer">
        <tr>
          <td class="bank">
            <div><span>Favorecido:</span><u>{escape(profile.get("favorecido") or "")}</u></div>
            <div><span>Banco:</span><u>{escape(profile.get("banco") or "")}</u></div>
            <div><span>Agência:</span><u>{escape(profile.get("agencia") or "")}</u></div>
            <div><span>Conta Corrente:</span><u>{escape(profile.get("conta_corrente") or "")}</u></div>
            <div><span>CPF / RG:</span><u>{escape(profile.get("cpf_rg") or "")}</u></div>
          </td>
          <td class="pay">
            <div><span>No Cheque</span><u></u></div>
            <div><span>Data pagamento</span><u></u></div>
            <div><span>Valor total</span><u>{_fmt_brl(grand_total)}</u></div>
          </td>
        </tr>
      </table>

      <table class="sign">
        <tr>
          <td><div class="sigline"></div><div class="siglbl">Assinatura</div></td>
          <td><div class="sigline"></div><div class="siglbl">Autorizado</div></td>
        </tr>
        <tr>
          <td><div class="sigline"></div><div class="siglbl">Data</div></td>
          <td><div class="sigline"></div><div class="siglbl">Data</div></td>
        </tr>
      </table>
    </section>
    """


def _receipts_html(receipts: list[dict]) -> str:
    if not receipts:
        return ""
    cards = []
    for rc in receipts:
        cap = f"{escape(rc['date'])} · {escape(rc['vendor'])}"
        amt = rc.get("amount")
        if amt is not None:
            sym = "R$" if (rc.get("currency") or "BRL") == "BRL" else (rc.get("currency") + " ")
            cap += f" · {sym} {_fmt_brl(amt)}"
        if rc.get("img"):
            inner = f'<img src="{rc["img"]}" alt="">'
        else:
            # Synthetic card for rows with no image (e.g. Uber CSV imports).
            inner = (
                '<div class="noimg">'
                f'<div class="noimg-v">{escape(rc["vendor"])}</div>'
                f'<div class="noimg-d">{escape(rc["date"])}</div>'
                '<div class="noimg-t">sem imagem</div>'
                "</div>"
            )
        cards.append(f'<figure class="rcard">{inner}<figcaption>{cap}</figcaption></figure>')
    return f'<section class="receipts"><div class="rgrid">{"".join(cards)}</div></section>'


_CSS = """
@page { size: A4; margin: 10mm 8mm; }
* { box-sizing: border-box; }
body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #111; font-size: 8.5px; }
.front { page-break-after: always; }

table { border-collapse: collapse; width: 100%; }
.header td { border: 1px solid #000; vertical-align: middle; padding: 2px 6px; }
.header .hl { width: 210px; text-align: center; }
.header .logo { height: 30px; }
.header .logo-text { font-weight: 700; font-size: 12px; }
.header .ht { font-size: 15px; font-weight: 600; text-align: center; }
.header .hc { width: 200px; font-size: 8px; }
.header .hc div { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid #ccc; padding: 1px 0; }
.header .hc div:last-child { border-bottom: none; }
.header .hc span { color: #444; }

.emp { margin-top: 4px; }
.emp td { border: 1px solid #000; padding: 3px 6px; height: 18px; }
.emp .lbl { background: #fff; text-align: right; font-weight: 600; white-space: nowrap; width: 1%; }
.emp .val { white-space: nowrap; }
.emp .chk { text-align: left; }
.emp .cbx { font-size: 11px; vertical-align: middle; }

.grid { margin-top: 4px; }
.grid th, .grid td { border: 1px solid #000; padding: 2px 4px; }
.grid thead th { background: #fff; font-weight: 700; text-align: center; vertical-align: middle; font-size: 8px; }
.grid th.cat { width: 8%; }
.grid td.dt { white-space: nowrap; width: 7%; }
.grid td.ct { width: 8%; text-align: center; }
.grid td.ds { width: 26%; }
.grid td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.grid td.tot { font-weight: 600; }
.grid td.pj { width: 7%; }
.grid tr.blank td { height: 16px; }
.grid tr.grand td { border-top: 2px solid #000; font-weight: 700; height: 20px; }
.noreceipt { color: #b00; font-style: italic; font-size: 7.5px; }

.footer { margin-top: 10px; }
.footer td { vertical-align: top; padding: 0 10px; border: none; }
.footer .bank { width: 62%; }
.footer .bank div, .footer .pay div { display: flex; gap: 6px; align-items: baseline; padding: 2px 0; }
.footer .bank span { white-space: nowrap; }
.footer u, .footer .bank u, .footer .pay u { flex: 1; border-bottom: 1px solid #000; min-height: 12px; text-decoration: none; padding-left: 4px; }
.footer .pay span { white-space: nowrap; min-width: 80px; }

.sign { margin-top: 22px; }
.sign td { width: 50%; padding: 0 24px; border: none; text-align: center; }
.sign .sigline { border-bottom: 1px solid #000; height: 26px; }
.sign .siglbl { font-weight: 700; margin-top: 2px; margin-bottom: 14px; }

.rgrid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.rcard { margin: 0; break-inside: avoid; border: 1px solid #ccc; border-radius: 3px; padding: 4px; display: flex; flex-direction: column; align-items: center; }
.rcard img { max-width: 100%; max-height: 240px; object-fit: contain; }
.rcard figcaption { font-size: 7.5px; color: #333; margin-top: 3px; text-align: center; word-break: break-word; }
.rcard .noimg { width: 100%; height: 150px; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #f6f6f6; border: 1px dashed #bbb; }
.noimg-v { font-weight: 700; font-size: 9px; }
.noimg-d { font-size: 8px; color: #555; }
.noimg-t { font-size: 7px; color: #999; margin-top: 4px; text-transform: uppercase; letter-spacing: .04em; }
.receipts h2 { font-size: 11px; margin: 0 0 6px; }
"""


def _resolve_profile(user_id: Optional[int], statement: dict) -> dict:
    """Pick the profile for the report's front page:
    1. explicit user_id (the picker on the modal)
    2. the user guessed from the statement's cardholder name
    3. the global saved default profile (backward compat)
    """
    if user_id is not None:
        u = report_users.get_user(user_id)
        if u:
            return {**report_profile.DEFAULT_PROFILE, **u}
    guessed = report_users.guess_user_for_statement(statement)
    if guessed:
        return {**report_profile.DEFAULT_PROFILE, **guessed}
    return report_profile.load_profile()


def build_statement_report(statement_id: int, user_id: Optional[int] = None) -> bytes:
    """Render the combined expense-report PDF for one statement. Returns the
    PDF as bytes. `user_id` selects a roster user's profile; if omitted we
    try to guess by cardholder name, then fall back to the global profile."""
    from weasyprint import HTML

    data = _assemble(statement_id)
    profile = _resolve_profile(user_id, data["statement"])

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        + _front_page_html(data, profile)
        + _receipts_html(data["receipts"])
        + "</body></html>"
    )
    pdf_bytes = HTML(string=html).write_pdf()
    logger.info(
        "Built report for statement %d (user=%s): %d rows, %d receipts, %d bytes",
        statement_id,
        profile.get("nome") or "—",
        len(data["rows"]),
        len(data["receipts"]),
        len(pdf_bytes),
    )
    return pdf_bytes


# ─── Reimbursement report (receipts only — no statement, no matching) ────────


def _assemble_reimbursement(receipts: list) -> dict[str, Any]:
    """Build the report data from a flat list of reimbursement receipts.

    Same shape as `_assemble` but: no statement, every row is "matched"
    (the receipt IS the proof — no justification note), and no IOF.
    """
    rows: list[dict] = []
    grid: list[dict] = []
    for r in receipts:
        meta = _receipt_meta(r)
        amount = float(meta.get("total_amount") or 0)
        label = meta.get("vendor_name") or r.get("description") or "Receipt"
        col = _bucket(meta.get("category"), label)
        rows.append(
            {
                "date": _fmt_date_br(meta.get("date")),
                "conta": "",
                "detalhe": label.upper().strip() or "—",
                "col": col,
                "amount": amount,
                "matched": True,  # reimbursement rows always have a receipt
            }
        )
        grid.append(
            {
                "vendor": label,
                "date": _fmt_date_br(meta.get("date")),
                "amount": meta.get("total_amount"),
                "currency": meta.get("currency") or "BRL",
                "img": _image_data_uri(r.get("filepath")),
            }
        )
    return {"statement": {"filename": None}, "rows": rows, "iof": None, "receipts": grid}


def build_reimbursement_report(
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    currency: Optional[str] = None,
) -> bytes:
    """Render a reimbursement report: the front-page form (Reembolso ticked)
    over all `reimbursement`-type receipts in the optional date range +
    currency, followed by their images. Raises ValueError if none match."""
    from weasyprint import HTML

    receipts = db.get_receipts_by_period(date_from, date_to, "reimbursement")
    if currency:
        cur = currency.upper()
        receipts = [r for r in receipts if (_receipt_meta(r).get("currency") or "").upper() == cur]
    if not receipts:
        raise ValueError("No reimbursement receipts match the selected filters")

    # Sort by date so the table reads chronologically.
    receipts.sort(key=lambda r: _receipt_meta(r).get("date") or "")

    data = _assemble_reimbursement(receipts)

    # Profile: explicit user → global default. No statement to guess from.
    if user_id is not None and report_users.get_user(user_id):
        profile = {**report_profile.DEFAULT_PROFILE, **report_users.get_user(user_id)}
    else:
        profile = report_profile.load_profile()
    # Force the reimbursement framing.
    profile = {**profile, "expense_kind": "reembolso"}
    if currency:
        profile["moeda"] = currency.upper() if currency.upper() != "BRL" else "R$"

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        + _front_page_html(data, profile)
        + _receipts_html(data["receipts"])
        + "</body></html>"
    )
    pdf_bytes = HTML(string=html).write_pdf()
    logger.info(
        "Built reimbursement report (user=%s, cur=%s): %d rows, %d receipts, %d bytes",
        profile.get("nome") or "—",
        currency or "all",
        len(data["rows"]),
        len(data["receipts"]),
        len(pdf_bytes),
    )
    return pdf_bytes
