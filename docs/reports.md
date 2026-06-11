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
they differ only in how the rows are assembled and a few front-page bits
(the card form shows Vencimento / Nº Cartão and a signature line; the
reimbursement form shows a "Reembolso de despesas" marker and a bank-
details footer). The reimbursement report has **no statement, no
matching, no IOF, and no open-item justification rows** — every receipt
is its own proof. See [Reimbursement report](#reimbursement-report) below.

Every generated PDF (both kinds) is saved and listed on the **Reports
tab** — see [History](#history).

## What the PDF contains

A single self-contained PDF mirroring the Atlantic Nickel
"Prestação de Contas - Cartão Corporativo" form
(code MMB-F-REG-0101-001-00):

**Front page**

- Header band: company logo + form title + Código / Página.
- Identity band: Nome · VENCIMENTO (due date) · Nº Cartão. (The
  reimbursement variant shows a "Reembolso de despesas" marker here
  instead of the card-specific fields.)
- Expense table columns: Data · Centro de Custo+Uneg · Conta Contábil ·
  Detalhamento da despesa, then **four category columns** —
  `Alimentação · Abastecimento/Estac./Taxi · Hospedagem · Outros` — and
  TOTAL. Each charge's amount lands in exactly one category column;
  columns total at the bottom, plus a grand total. Rows are ordered by
  **date ascending** (each keeps its transaction index for editing).
- **Detalhamento** is an auto label from the charge's category (UBER,
  ALIMENTAÇÃO, HOSPEDAGEM…) and **Centro de Custo+Uneg** comes from the
  matched receipt / a per-line override / the profile default — all
  editable (see [Per-line report fields](#per-line-report-fields) and the
  [Review & edit modal](#review--edit-before-export)).
- **Open items** (transactions with no matched receipt) render with the
  Detalhamento cell tagged `(sem comprovante — justificativa: ____)`.
  If the user typed a **justification** on the Reconcile board (see
  [Justifying open items](#justifying-open-items)), that text prints in
  place of the blank line.
- **Estornos / credits** (negative statement rows) print with their
  negative amount and an `(estorno / crédito)` marker instead of the
  justification blank — a refund nets its category column and the
  TOTAL, and never expects a receipt.
- **Totals.** Statement-report amounts are all BRL (the card's bill
  amount). The **reimbursement** report converts every receipt to BRL at
  the latest FX rate and prints **one** `TOTAL` — a single payable
  amount; each converted row keeps its original currency + amount in
  Detalhamento (e.g. `· CAD 8,00`) for audit. A currency with no
  available rate falls back to its own `TOTAL (XYZ)` bucket rather than
  being silently summed into BRL.
- **IOF row**: Brazilian tax on international transactions, pulled live
  from the saved statement PDF (the parser drops it from the txn list,
  but the form shows it as a line in Outros).
- Footer: a signature line — Data + "Assinatura {nome} + Aprovador
  imediato". The reimbursement variant instead shows a bank-details
  footer (Favorecido / Banco / Agência / Conta Corrente / CPF-RG) so the
  payee can be reimbursed.

**Open-items page** (only when there are unmatched charges)

- Sits between the front-page spreadsheet and the receipt grid.
- Heading "Lançamentos sem comprovante" + a count and total, then one
  bordered block per card charge with no receipt: date · description ·
  category column · BRL amount, with **Justificativa:** below — the
  typed note (italic), or a blank underline if none was entered.
- Credit/estorno rows are excluded — a refund isn't a missing receipt.
- `_open_items_html()`; renders nothing when every charge is matched.

**Receipt pages**

- Every matched receipt as an image in a **3-column table** (3 cards per
  `<tr>`, `break-inside: avoid` on the row). A table is used rather than
  CSS grid because WeasyPrint paginates table rows reliably — a row of 3
  image+caption cells moves to the next page intact instead of splitting
  an image from its caption. Packs ~9 small receipts per page, fewer for
  tall ones.
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

| Form column                   | Mapped from                                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| Alimentação                   | `meals`                                                                                            |
| Abastecimento / Estac. / Taxi | `fuel`, `transportation`, or parking/fuel keywords (estacion, posto, parking, parkchamp, shell, …) |
| Hospedagem                    | `lodging`                                                                                          |
| Outros                        | `travel`, `office_supplies`, `other`, unknown, and IOF                                             |

`meals` wins first; fuel, transport and the parking/fuel keyword check
all fold into the single Abastecimento column. Mapping logic is
`expense_report_pdf._bucket()`.

### Conta Contábil (GL accounts)

The report's **Conta Contábil** column shows a different GL account per
expense column. These are **company-wide** — the same chart of accounts
for every cardholder — so they live in the global config
(`report_profile.json`), not on individual users.

- Five fields: a default (`conta_contabil`) plus one per column
  (`conta_alimentacao`, `conta_abastecimento`, `conta_hospedagem`,
  `conta_outros`). A row uses its column's account, falling back to the
  default when that's blank. (Legacy `conta_combustivel` / `conta_taxi` /
  `conta_material` are migrated to `conta_abastecimento` on load.)
- Edit them — plus the default **Centro de Custo+Uneg** — on the
  **Reports tab → Accounts** ("Contas & centro de custo" modal), saved
  via `PUT /api/report-profile`.
- At build time `_with_global_accounts()` overlays these onto whichever
  cardholder generated the report, so they're identical regardless of
  who it's for.

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
Fuel/Parking and Transport both reach the Abastecimento column; Lodging
reaches Hospedagem. Matched cards show the receipt's category as a locked
badge instead.

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

## Per-line report fields

- **Detalhamento da despesa** — the label printed in the Detalhamento
  column. Precedence: a per-line override → the **matched receipt's own
  description** when it's meaningful (placeholders like `imported from
folder scan` are skipped) → an auto label from the charge's category
  (`UBER`, `ALIMENTAÇÃO`, `HOSPEDAGEM`, `ABASTECIMENTO`…). Editable per
  line on the Reconcile board (every card) and in the Review-&-edit
  modal; type to override for specifics like `MOTORISTA RAYLANDER`.
  Stored in `data/statement_labels.json`; endpoint
  `PUT /api/statements/{id}/transactions/{index}/detalhe` `{detalhe}`.
  The **open-items page** still shows the raw bank descriptor (not the
  auto label) so an unmatched charge stays identifiable.

- **Centro de Custo+Uneg** — precedence per line:
  1. **Matched** → the matched receipt(s)' `cost_centers` (set on the
     Receipts tab; comma-separate to **list several** on the line for a
     split bill — multiple receipts on one charge are joined, deduped).
     On the Reconcile board a matched card shows this read-only (a chip).
  2. **Open item** (no receipt) → the per-line board override
     (`data/statement_costcenters.json`,
     `PUT …/transactions/{index}/cost-center`).
  3. Either, when blank → the cardholder profile `centro_custo` default.

  The **Review & edit** modal (below) can edit CC on any row — it routes
  a matched row's edit to the receipt, an open item's to the line override.

The form header's **Vencimento** (due date) is set when generating the
report (the Generate-report / Review-&-edit flow has a date field) and
passed as the `vencimento` query param — it isn't stored on the statement.

## Review & edit before export

The **Generate report** modal has a **Review & edit** button (alongside
**Download PDF**). It opens a table of the assembled front-page rows —
Data · Centro de Custo+Uneg · Conta · Detalhamento · Coluna · Valor — with
live per-column totals. Editable cells: **Detalhamento**, **Centro de
Custo+Uneg**, and **Coluna** (the category column). Amount, date and Conta
are read-only (reconciled from the statement).

Edits **persist** through the same stores the board uses, so they stick
and also show on the board: Detalhamento → the label override; for
**Centro de Custo** and **Coluna**, a matched row writes to the
receipt(s) (`PATCH /api/receipts/{id}` — `cost_centers` / `category`) and
an open item writes the per-line override. **Export PDF** then builds the
report, which re-derives the same values. Backed by
`GET /api/statements/{id}/report-rows` (see [api.md](api.md)).

## XLSX export

Both the **Generate report** modal (**Download XLSX**) and the **Review &
edit** modal (**Export XLSX**) can download the front-page table as an
editable `.xlsx` — same layout and numbers as the PDF front page (header
band + the categorised table with live `=SUM` totals + signature line),
but **no receipt images**, and built with `openpyxl`. Useful when the
finance team needs to tweak the spreadsheet in Excel. Endpoint
`GET /api/statements/{id}/report.xlsx`; not saved to report history.

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
  amount). The Detalhamento is the vendor name (not an auto label). Every
  row is "matched" — the receipt is the proof, so there's no
  `(sem comprovante)` justification line and no IOF.
- The identity band shows a **Reembolso de despesas** marker instead of
  the card's Vencimento / Nº Cartão, and the footer carries the
  bank-details block (Favorecido / Banco / …) so the payee can be paid.
- Receipt grid is the same — each reimbursement receipt's image.

**Currencies → one BRL total.** The report includes **all**
reimbursement receipts and converts each to BRL at the latest FX rate,
so the form yields a single payable `TOTAL` in R$. Each non-BRL row keeps
its original currency + amount in Detalhamento (`· CAD 8,00`) for audit.
The modal's currency dropdown defaults to **All currencies → BRL**; the
per-currency entries are optional scoping (still converted to BRL). A
currency with no rate available falls back to its own `TOTAL (XYZ)`
bucket so nothing is hidden. (FX uses the **latest** rate, not the rate
as of each receipt's date.)

The modal also takes an optional **date range** and a **cardholder**
(for the name + bank footer). A live "N in scope" count shows how many
receipts the current filters select and disables Download at zero;
already-`reimbursed` receipts (from a completed report) are excluded.

Code: `expense_report_pdf.build_reimbursement_report(user_id, date_from,
date_to, currency)`. Selection is `_select_reimbursement_receipts` —
`get_receipts_by_period(..., "reimbursement")`, **excluding already-
`reimbursed`** receipts and the optional currency filter — then
`fetch_rates(..., "BRL")` + `convert` per row, reusing the shared
renderers. `reimbursement_receipt_ids(...)` returns that same selection's
ids, recorded with the report so it can be marked completed (below).

### Marking a report completed

Once a reimbursement report is **issued and marked completed**, the
receipts it covered are settled — they shouldn't sit on the active board
or be pulled into a later report. The report-history row (see
[History](#history)) shows a **Mark completed** button; clicking it flips
those receipts' `match_status` to **`reimbursed`** — a purple badge,
hidden from the Active filter, counted in the **Reimbursed** topbar stat,
and excluded from future reimbursement reports. **Reopen** reverts them
to `pending`. The link is the `receipt_ids` stored on the report at
generation, so only reports created after this feature carry the button.
Endpoint: `POST /api/report-history/{id}/complete` `{completed}` →
`report_history.set_completed` + `db.update_receipts_match_status(ids,
"reimbursed" | "pending")`.

## History

Every PDF the app builds — statement _and_ reimbursement — is saved and
listed on the **Reports tab**. Each row shows a kind badge, a title
(e.g. `Statement · LUIZ EDUARDO PERSECHINI` or
`Reimbursement · TONY · BRL`), the period/currency/size/timestamp, and
**Download** / **Del** buttons. Reimbursement rows also get **Mark
completed** / **Completed ✓** / **Reopen** (see [Marking a report
completed](#marking-a-report-completed)).

Persistence is `report_history.py`: the PDF bytes are written to
`data/reports/report_{id}.pdf` and indexed in
`data/reports/history.json` (newest first, monotonic never-reused ids,
gitignored — the files embed receipt images + PII). Both report
endpoints call `report_history.record(...)` after building, labelling
the entry with the resolved cardholder name
(`expense_report_pdf.cardholder_name`). History is **capped at 100**
records — the oldest entry and its PDF are pruned when a new one lands,
so `data/reports/` can't grow forever. Reimbursement records also store
`receipt_ids` (which receipts went in) plus a `completed` flag +
`completed_at`, driving the Mark-completed / Reopen actions and the
`reimbursed` status.

## Endpoints

| Method | Path                                   | Purpose                                                                       |
| ------ | -------------------------------------- | ----------------------------------------------------------------------------- |
| GET    | `/api/report-profile`                  | global default front-page fields                                              |
| PUT    | `/api/report-profile`                  | save the global default                                                       |
| GET    | `/api/report-users`                    | list cardholders                                                              |
| POST   | `/api/report-users`                    | create a cardholder                                                           |
| PUT    | `/api/report-users/{id}`               | update a cardholder                                                           |
| DELETE | `/api/report-users/{id}`               | delete a cardholder                                                           |
| GET    | `/api/statements/{id}/guess-user`      | best-match cardholder for a statement                                         |
| GET    | `/api/statements/{id}/report?user_id=` | build + stream the statement PDF                                              |
| GET    | `/api/reports/reimbursement?...`       | build + stream the reimbursement PDF                                          |
| GET    | `/api/report-history`                  | list saved reports (newest first)                                             |
| GET    | `/api/report-history/{id}/download`    | re-download a saved report                                                    |
| DELETE | `/api/report-history/{id}`             | delete a saved report + its file                                              |
| POST   | `/api/report-history/{id}/complete`    | mark a reimbursement report completed → its receipts `reimbursed` (or reopen) |

`/api/reports/reimbursement` takes `user_id`, `date_from`, `date_to`,
`currency` (all optional); it always converts to BRL — `currency` only
scopes which receipts. See [`api.md`](api.md) for request/response
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
