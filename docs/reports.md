# Expense reports & cardholders

How the combined "Prestação de Contas" PDF is built from a reconciled
statement, and how the cardholder roster auto-fills it.

There are **two** report systems in the app, for different needs:

|         | Excel reports                                | Combined PDF report                  |
| ------- | -------------------------------------------- | ------------------------------------ |
| Trigger | Reconcile tab → report-panel "Export Report" | Reconcile header → "Generate report" |
| Scope   | receipts across a period, FX-converted       | one reconciled statement             |
| Output  | `.xlsx` (editable)                           | `.pdf` (front page + receipt images) |
| Code    | `services/report_generator.py`               | `services/expense_report_pdf.py`     |

This doc covers the **combined PDF** — the corporate form deliverable.
The Excel path is the older period-based export and is unchanged.

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
  Detalhamento cell tagged `(sem comprovante — justificativa: ____)` so
  the reviewer hand-writes a reason.
- **IOF row**: Brazilian tax on international transactions, pulled live
  from the saved statement PDF (the parser drops it from the txn list,
  but the form shows it as a line in Outros).
- Footer: bank details (Favorecido / Banco / Agência / Conta Corrente /
  CPF-RG) + No Cheque / Data pagamento / Valor total, then
  **Assinatura / Autorizado / Data** signature lines.

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

| Form column                 | Mapped from                                                                          |
| --------------------------- | ------------------------------------------------------------------------------------ |
| Combustível, Estacionamento | parking / fuel keywords (estacion, posto, parking, parkchamp, shell, …) in the label |
| Despesas com alimentação    | `meals`                                                                              |
| Taxi / Correios             | `transportation` (uber, taxi, lyft)                                                  |
| Material para escritório    | `office_supplies`                                                                    |
| Outros                      | `travel`, `lodging`, `other`, unknown, and IOF                                       |

The parking/fuel keyword check runs first so a `transportation` charge
that's actually parking lands in the right column. Mapping logic is
`expense_report_pdf._bucket()`.

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
| GET    | `/api/statements/{id}/report?user_id=` | build + stream the combined PDF       |

See [`api.md`](api.md) for request/response shapes.

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
