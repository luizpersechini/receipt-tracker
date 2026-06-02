# Expense reports & cardholders

How the combined "Prestação de Contas" PDF is built from a reconciled
statement, and how the cardholder roster auto-fills it.

There are **three** report systems in the app, for different needs:

|         | Excel reports                  | Statement PDF report                 | Reimbursement PDF report             |
| ------- | ------------------------------ | ------------------------------------ | ------------------------------------ |
| Trigger | Reconcile → "Export Report"    | Reconcile header → "Generate report" | Reports tab → "Reimbursement report" |
| Scope   | receipts in a period, FX-conv. | one reconciled statement             | `reimbursement`-type receipts        |
| Output  | `.xlsx` (editable)             | `.pdf` (front page + receipts)       | `.pdf` (front page + receipts)       |
| Code    | `report_generator.py`          | `expense_report_pdf.py`              | `expense_report_pdf.py`              |

This doc covers the **two PDF reports** — the corporate form deliverable.
The Excel path is the older period-based export and is unchanged.

The two PDFs share all rendering (`_front_page_html` + `_receipts_html`);
they differ only in how the rows are assembled and which checkbox is
ticked (`Cartão Crédito` vs `Reembolso despesa`). The reimbursement
report has **no statement, no matching, no IOF, and no open-item
justification rows** — every receipt is its own proof. See
[Reimbursement report](#reimbursement-report) below.

Every generated PDF (both kinds) is saved and listed on the **Reports
tab** — see [History](#history).

## What the PDF contains

A single self-contained PDF mirroring the Atlantic Nickel
"Prestação de Contas - Cartão Corporativo" form:

**Front page**

- Header band: company logo + form title + Código / Data / Página.
- Employee band: Nome · Data · C/C (cost centre) · Motivo do Gasto ·
  Local · Moeda, plus the Reembolso / Cartão Crédito checkbox.
- Expense table: one row per statement transaction. Each charge's
  amount lands in **one of five category columns**:
  `Combustível-Estacionamento · Despesas com alimentação · Taxi-Correios
· Material para escritório · Outros`. Columns total at the bottom,
  plus a grand total.
- **Open items** (transactions with no matched receipt) render with the
  Detalhamento cell tagged `(sem comprovante — justificativa: ____)`.
  If the user typed a **justification** on the Reconcile board (see
  [Justifying open items](#justifying-open-items)), that text prints in
  place of the blank line.
- **IOF row**: Brazilian tax on international transactions, pulled live
  from the saved statement PDF (the parser drops it from the txn list,
  but the form shows it as a line in Outros).
- Footer: bank details (Favorecido / Banco / Agência / Conta Corrente /
  CPF-RG) + No Cheque / Data pagamento / Valor total, then
  **Assinatura / Autorizado / Data** signature lines.

**Open-items page** (only when there are unmatched charges)

- Sits between the front-page spreadsheet and the receipt grid.
- Heading "Lançamentos sem comprovante" + a count and total, then one
  bordered block per card charge with no receipt: date · description ·
  category column · BRL amount, with **Justificativa:** below — the
  typed note (italic), or a blank underline if none was entered.
- `_open_items_html()`; renders nothing when every charge is matched.

**Receipt pages**

- Every matched receipt as an image in a **3-column CSS grid**.
  `break-inside: avoid` keeps each image with its caption, so the layout
  packs ~9 small receipts per page and fewer for tall ones — "as many as
  fit while readable".
- Caption under each: `date · vendor · amount`.
- Real image files are embedded as downscaled JPEG data URIs (long edge
  ≤ 1000 px, quality 72) so the PDF is self-contained but not huge.
  HEIC → JPEG, PDF → first-page raster. Uber/Gmail rows with **no file**
  render a "sem imagem" synthetic card (vendor + date) instead.

Rendering is HTML/CSS → **WeasyPrint**. The whole document is one HTML
string; WeasyPrint paginates the long transaction table and the receipt
grid automatically.

## Category mapping

Each charge's receipt/transaction category maps onto a form column:

| Form column                 | Mapped from                                                                      |
| --------------------------- | -------------------------------------------------------------------------------- |
| Combustível, Estacionamento | `fuel`, or parking/fuel keywords (estacion, posto, parking, parkchamp, shell, …) |
| Despesas com alimentação    | `meals`                                                                          |
| Taxi / Correios             | `transportation` (uber, taxi, lyft)                                              |
| Material para escritório    | `office_supplies`                                                                |
| Outros                      | `travel`, `lodging`, `other`, unknown, and IOF                                   |

The parking/fuel keyword check runs first so a `transportation` charge
that's actually parking lands in the right column. Mapping logic is
`expense_report_pdf._bucket()`.

**Which category a charge uses** (precedence in `_assemble`):

1. **Matched → the receipt's category wins.** Pair a receipt and its
   category drives the column, regardless of any override.
2. Else the user's **override** (set on the Reconcile board — see
   [Editing a charge's category](#editing-a-charges-category)).
3. Else the **parser's** guess.

So to fix where a matched charge lands, change the _receipt's_ category
(Receipts tab → Edit); to fix an open item, set the override on the
board.

### Editing a charge's category

On the Reconcile board, each **unmatched** transaction card has a
category dropdown (Meals · Transport · Fuel/Parking · Travel · Lodging ·
Office · Other) — pick one to move the charge to another report column.
The `fuel` option reaches Combustível/Estacionamento directly. Matched
cards show the receipt's category as a locked badge instead.

- Overrides are keyed by `(statement_id, transaction_index)` in
  `data/statement_categories.json` (gitignored), survive re-parses, and
  are cleared when the statement is deleted.
- Endpoint: `PUT /api/statements/{id}/transactions/{index}/category`
  `{category}`. The effective category + a `category_locked` flag come
  back on each transaction from `GET …/transactions`.

## Justifying open items

A statement charge with no receipt (lost receipt, or an unidentified
expense) is an **open item**. On the **Reconcile board**, each unmatched
transaction card has a `+ justification (no receipt)` field — type why
there's no receipt and it prints on the report's front page in place of
the blank `justificativa: ____` line.

- Notes are keyed by `(statement_id, transaction_index)` and stored in
  `data/statement_notes.json` (gitignored), separate from the parsed
  transactions so a re-parse doesn't wipe them.
- Save with the Save button or Cmd/Ctrl+Enter; Esc cancels. A saved note
  shows in amber with a pencil; click to edit.
- Endpoint: `PUT /api/statements/{id}/transactions/{index}/note`
  `{note}`. The note also comes back on each transaction from
  `GET /api/statements/{id}/transactions`.
- Deleting the statement clears its notes.

## Cardholders (Users tab)

A company has several cardholders, each with their own name, cost centre
and bank details. The **Users tab** is the roster:

- Add / Edit / Delete cardholders. Each user is a full report profile —
  the same fields the front page needs (nome, C/C, conta contábil,
  motivo, local, favorecido, banco, agência, conta corrente, CPF/RG,
  …).
- Stored in `data/report_users.json` (gitignored — it holds CPF/RG and
  bank account numbers).
- New users pre-fill from the global default template
  (`report_profile.json`), so company-level fields (form code, company
  name) are inherited; you only fill the person-specific ones.
- IDs are monotonic (`max(existing)+1`); deleting a user never reuses
  their id.

### Name auto-detection

When you click **Generate report**, the app guesses which cardholder the
statement belongs to:

1. Build a candidate text blob: the statement **filename** (with the
   saved `statement_YYYYMMDD_HHMMSS_` prefix stripped) + the **first
   page of the statement PDF** (Itaú prints the holder name up top).
2. Tokenise it, dropping pt-BR particles and generic statement words
   (`de`, `da`, `cartão`, `fatura`, `itaú`, `final`, `visa`, …).
3. For each roster user, score the overlap between the user's name
   tokens and the candidate tokens. A match must include at least one
   **distinctive** token (length ≥ 4) so a bare common first name can't
   win on its own.
4. Highest score wins; ties and no-confident-match return nothing.

Logic is `report_users.guess_user_for_statement()`. Examples:

- `statement_20260526_144911_LUIZ PERSECHINI 22.700,57.pdf` → the user
  whose name contains "Persechini".
- `Cartão corporativo Tony 20MAI2026.pdf` → the user named "Tony …".
- `random_statement.csv` → no match (picker stays on defaults).

## The generate flow

```
Reconcile header → "Generate report"
  → GET /api/report-users                  (populate the picker)
  → GET /api/statements/{id}/guess-user    (pre-select the cardholder)
  → user confirms / overrides the dropdown
  → GET /api/statements/{id}/report?user_id=N
       → expense_report_pdf.build_statement_report(id, user_id)
           → _assemble(id)            load statement + matches + receipts
           → _resolve_profile(...)    user_id → guess → global default
           → render HTML → WeasyPrint → PDF bytes
  → browser downloads prestacao_contas_statement_{id}.pdf
```

**Profile precedence** in `_resolve_profile`:

1. explicit `user_id` (the picker)
2. the user guessed from the statement name
3. the global `report_profile.json` (back-compat fallback)

## Reimbursement report

Personal-spend receipts (the ones you mark **reimbursement** with the
per-row type toggle) never appear on a corporate statement, so they
can't be reconciled. The **Reports tab → "Reimbursement report"**
button builds the same form from those receipts directly:

- One row per reimbursement receipt (date · vendor · category column ·
  amount). Every row is "matched" — the receipt is the proof, so there's
  no `(sem comprovante)` justification line and no IOF.
- The **Reembolso despesa** checkbox is ticked instead of Cartão Crédito.
- Receipt grid is the same — each reimbursement receipt's image.

**Single currency per report.** The form has one Total / Moeda column,
so the modal scopes to one currency (the dropdown defaults to the most
common currency among your reimbursement receipts, listed with counts
like `BRL (10)`). This keeps the total correct when you have
mixed-currency reimbursements — generate one report per currency.

The modal also takes an optional **date range** and a **cardholder**
(for the name + bank footer). A live "N in scope" count shows how many
receipts the current filters select and disables Download at zero.

Code: `expense_report_pdf.build_reimbursement_report(user_id, date_from,
date_to, currency)`. It pulls `get_receipts_by_period(..., "reimbursement")`,
filters currency in Python, and reuses the shared renderers.

## History

Every PDF the app builds — statement _and_ reimbursement — is saved and
listed on the **Reports tab**. Each row shows a kind badge, a title
(e.g. `Statement · LUIZ EDUARDO PERSECHINI` or
`Reimbursement · TONY · BRL`), the period/currency/size/timestamp, and
**Download** / **Del** buttons.

Persistence is `report_history.py`: the PDF bytes are written to
`data/reports/report_{id}.pdf` and indexed in
`data/reports/history.json` (newest first, monotonic ids, gitignored —
the files embed receipt images + PII). Both report endpoints call
`report_history.record(...)` after building, labelling the entry with
the resolved cardholder name (`expense_report_pdf.cardholder_name`).

## Endpoints

| Method | Path                                   | Purpose                               |
| ------ | -------------------------------------- | ------------------------------------- |
| GET    | `/api/report-profile`                  | global default front-page fields      |
| PUT    | `/api/report-profile`                  | save the global default               |
| GET    | `/api/report-users`                    | list cardholders                      |
| POST   | `/api/report-users`                    | create a cardholder                   |
| PUT    | `/api/report-users/{id}`               | update a cardholder                   |
| DELETE | `/api/report-users/{id}`               | delete a cardholder                   |
| GET    | `/api/statements/{id}/guess-user`      | best-match cardholder for a statement |
| GET    | `/api/statements/{id}/report?user_id=` | build + stream the statement PDF      |
| GET    | `/api/reports/reimbursement?...`       | build + stream the reimbursement PDF  |
| GET    | `/api/report-history`                  | list saved reports (newest first)     |
| GET    | `/api/report-history/{id}/download`    | re-download a saved report            |
| DELETE | `/api/report-history/{id}`             | delete a saved report + its file      |

`/api/reports/reimbursement` takes `user_id`, `date_from`, `date_to`,
`currency` (all optional). See [`api.md`](api.md) for request/response
shapes.

## Gotchas

- **Amounts are BRL.** The front page uses each transaction's BRL bill
  amount (what the card charged), even for international rows. The native
  currency still shows on the receipt pages and in the reconcile board.
- **A statement that isn't reconciled** still generates — every charge
  becomes a row; unmatched ones just get the justification note and no
  receipt image. So you can print the form first and fill receipts later.
- **WeasyPrint speed**: ~4 s for a statement with ~90 receipt images.
  The modal shows a spinner ("Building PDF…") meanwhile.
- **The logo** is `static/report_assets/logo.png`, embedded as a data
  URI at build time. Swap that file to rebrand; no code change needed.
- **Motivo / Local are per-user** in v1 (stored on the cardholder). If
  they vary per report, edit the user before generating — a per-report
  override field is a future addition.
