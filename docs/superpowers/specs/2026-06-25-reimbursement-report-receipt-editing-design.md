# Editing receipts on an issued reimbursement report

**Date:** 2026-06-25
**Status:** Approved design — ready for implementation plan
**Approach:** A — extend the existing `report_history` record (chosen over a new
"claim" entity (B) and receipt-owned membership (C)).

## Goal

After a reimbursement report has been issued, let the user **add and remove
existing reimbursement receipts** on it, at any stage — including after the
report has been marked completed — then **re-issue** the PDF on demand.

Scope is deliberately narrow (set-membership only):

- **In scope:** add/remove reimbursement-type receipts that are already
  uploaded; explicit PDF re-issue with a version stamp; status sync on
  completed reports.
- **Out of scope (this feature):** uploading a new receipt directly into a
  report; manual expense lines with no receipt; multi-report membership.

## Dependency / branch note

This feature **builds on PR #2 (`feature/report-editor`)**, which adds
`report_history` records carrying `receipt_ids` + `completed`/`completed_at`,
the `POST /report-history/{id}/complete` flow, the BRL-conversion reimbursement
builder, and the `reimbursed` `match_status`. That branch is **not yet merged**
and is **not on the current branch** (`claude/project-compatibility-check-g7lau0`).
Resolve before implementation — recommended: merge PR #2 to `main`, branch this
work from there. The design assumes PR #2's code is present.

## Core invariant

> A receipt is `reimbursed` **if and only if** it is a member of a **completed**
> report. A receipt belongs to **at most one** report.

Every behaviour below exists to keep that invariant true.

## Data model

Extend each reimbursement `report_history` record (stored in
`data/reports/history.json`):

| Field           | Meaning                                                                           |
| --------------- | --------------------------------------------------------------------------------- |
| `receipt_ids`   | already present; now the **mutable** membership set                               |
| `version`       | int; `1` at first issue, `+1` on each re-issue                                    |
| `pdf_stale`     | `true` after an edit, `false` after re-issue → drives the "PDF out of date" badge |
| `updated_at`    | timestamp of the last membership edit                                             |
| `file` / `size` | overwritten on re-issue                                                           |

New reports initialize `version=1`, `pdf_stale=false` when the generate flow
records them. Records written before this feature have neither; treat missing
`version` as `1` and missing `pdf_stale` as `false`.

## Status truth-table

| Report state               | Add receipt                   | Remove receipt              |
| -------------------------- | ----------------------------- | --------------------------- |
| not completed              | join set · stays `pending`    | leave set · stays `pending` |
| **completed**              | join set · → **`reimbursed`** | leave set · → **`pending`** |
| _Complete_ (existing flow) | all members → `reimbursed`    | —                           |
| _Reopen_ (existing flow)   | —                             | all members → `pending`     |

Two changes that keep the invariant honest:

1. **The "add" picker only offers reimbursement-type receipts that are on no
   report** (single membership; receipts reimbursed elsewhere are excluded
   until freed by removal).
2. **Deleting a _completed_ report reverts its receipts to `pending`** — a fix
   to the existing delete, which would otherwise orphan them as `reimbursed`
   with no report.

## API (extends `src/routers/reports.py`)

- `GET /api/report-history/{id}` — the record + member receipt details +
  the **addable pool** (reimbursement receipts on no report), to drive the
  detail view.
- `POST /api/report-history/{id}/receipts` — body `{add: [ids], remove: [ids]}`.
  Edits the set; applies the status flips **iff** the report is completed; sets
  `pdf_stale=true`, `updated_at`. Returns the updated record plus a `rejected`
  list with reasons (e.g. "already on report #N", "not a reimbursement
  receipt").
- `POST /api/report-history/{id}/reissue` — regenerate the PDF from the current
  `receipt_ids`, `version += 1`, `pdf_stale=false`, overwrite `file`/`size`.
  **400 if the set is empty.**

Existing endpoints (`/complete`, `/download`, `DELETE`) are unchanged except the
delete-reverts-on-completed fix above.

## Builder change (`src/services/expense_report_pdf.py`)

Add `build_reimbursement_report_from_ids(ids, user_id=None) -> bytes` that
renders an **explicit** receipt-id list. It reuses the existing
`_assemble_reimbursement` + shared renderers + BRL conversion, so totals and
formatting are identical to the query-based path. The original
`build_reimbursement_report` (date/currency query) is unchanged and still
records the resulting `receipt_ids` at generation.

## UI (reuses the reimbursement-modal styles in `static/`)

- Reimbursement rows in report history get a **Manage** action → a report-detail
  view (modal or panel).
- The detail view shows:
  - **Member receipts** — date · vendor · amount + thumbnail, each with a
    **✕ remove**.
  - **Add receipts** — a multi-select picker of the addable pool.
  - **Live BRL total** of the current set.
  - **Download** (current PDF) and **Re-issue PDF**, where Re-issue is
    highlighted with a **"PDF out of date · v{n}"** badge whenever `pdf_stale`.

## Edge cases

- Add a receipt already on another report → **rejected** with reason (picker
  prevents it; the endpoint still guards).
- Add a non-reimbursement-type receipt → guarded (picker only lists
  reimbursement-type).
- Remove the last receipt → allowed (empty set), but **re-issue of an empty
  report is blocked** (400); the row flags it empty.
- Complete a report whose PDF is stale → completion flips statuses by
  membership regardless; `pdf_stale` stays until the user re-issues (badge
  reminds).
- FX offline on re-issue → same behaviour as generation today (latest rate;
  per-currency fallback when unreachable). Inherited, not new.
- Concurrency → `report_history` writes are already `@locked`.

## Testing

Unit tests on the existing `hist_env` (report-history store) and `report_env`
(throwaway DB) fixtures:

- add/remove mutates `receipt_ids`, sets `pdf_stale`, leaves `version` until
  re-issue;
- single-membership rejection;
- the status truth-table — add/remove on completed vs. not-completed reports;
- re-issue regenerates the PDF, bumps `version`, clears `pdf_stale`, and **blocks
  an empty set**;
- delete of a completed report reverts its receipts to `pending`.

Plus one end-to-end: generate → add → re-issue → complete → remove → assert
receipt statuses follow the truth-table.

## Out of scope

Uploading a new receipt into a report; manual receiptless expense lines;
multi-report membership; approval workflow. (These are the Approach-B "claim"
features, intentionally deferred.)
