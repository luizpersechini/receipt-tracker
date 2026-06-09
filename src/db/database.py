"""SQLite database for receipt tracking."""

import json
import sqlite3
from typing import Optional

from ..config import settings


def get_connection():
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    # Writers arrive from many threads (uvicorn's to_thread pool, the folder
    # watcher, Gmail sync). WAL lets readers proceed during a write, and the
    # busy timeout makes bursts queue instead of raising "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        metadata TEXT,
        description TEXT,
        match_status TEXT DEFAULT 'pending',
        expense_type TEXT DEFAULT 'corp_card',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS statements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        transactions TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id INTEGER,
        statement_id INTEGER,
        transaction_data TEXT,
        match_score REAL,
        matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (receipt_id) REFERENCES receipts (id),
        FOREIGN KEY (statement_id) REFERENCES statements (id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expense_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        report_type TEXT NOT NULL,
        target_currency TEXT DEFAULT 'BRL',
        period_start TEXT,
        period_end TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fx_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        from_currency TEXT NOT NULL,
        to_currency TEXT NOT NULL,
        rate REAL NOT NULL,
        UNIQUE(date, from_currency, to_currency)
    )
    """)

    conn.commit()

    for table, col, definition in [
        ("receipts", "match_status", "TEXT DEFAULT 'pending'"),
        ("receipts", "expense_type", "TEXT DEFAULT 'corp_card'"),
        # Manual / auto pairing on the reconcile board. `transaction_index`
        # pins a match to a specific parsed-statement row (the JSON blob in
        # `transaction_data` has no row identity on its own). `match_kind`
        # distinguishes auto-matches (engine output) from user drag-drops.
        ("matches", "match_kind", "TEXT DEFAULT 'auto'"),
        ("matches", "transaction_index", "INTEGER"),
        # Mark a reconciled statement "complete" → archived=1 hides it from
        # the active statements list (its receipts stay matched/hidden).
        ("statements", "archived", "INTEGER DEFAULT 0"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()


# Receipts


def save_receipt(
    filename: str, filepath: str, metadata: dict, description: Optional[str] = None
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO receipts (filename, filepath, metadata, description) VALUES (?, ?, ?, ?)",
        (filename, filepath, json.dumps(metadata), description),
    )
    receipt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return receipt_id


def get_all_receipts() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM receipts ORDER BY created_at DESC")
    receipts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return receipts


def get_receipt_by_filename(filename: str) -> Optional[dict]:
    """Look up a receipt by its stored filename. Used for dedup on bulk imports."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM receipts WHERE filename = ? LIMIT 1", (filename,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_receipt_by_id(receipt_id: int) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_receipts_by_type(expense_type: str) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM receipts WHERE expense_type = ? ORDER BY created_at DESC", (expense_type,)
    )
    receipts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return receipts


def get_receipts_by_period(
    period_start: Optional[str], period_end: Optional[str], expense_type: Optional[str] = None
) -> list:
    all_r = get_all_receipts()
    result = []
    for r in all_r:
        if expense_type and r.get("expense_type") != expense_type:
            continue
        meta = json.loads(r.get("metadata") or "{}")
        d = meta.get("date")
        if period_start and d and d < period_start:
            continue
        if period_end and d and d > period_end:
            continue
        result.append(r)
    return result


def find_uber_receipt_by_date_amount(
    date: str,
    amount: float,
    currency: str,
    tolerance: float = 0.01,
    source: Optional[str] = None,
) -> Optional[dict]:
    """
    Find an existing Uber-imported receipt that matches the given
    date / amount (within tolerance) / currency tuple. Used for cross-source
    deduplication when the same ride is imported via both CSV and Gmail.

    `source` restricts the search to rows ingested by that source ("csv" or
    "gmail"; rows without an `uber_source` field are legacy CSV imports).
    Cross-source dedup MUST pass the *other* source — otherwise two legitimate
    same-day same-fare rides from a single import dedupe against each other
    and the second ride is silently dropped.
    """
    if not date or amount is None or not currency:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM receipts WHERE filename LIKE 'uber_%' ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()

    cur_upper = currency.upper()
    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            continue
        if source and (meta.get("uber_source") or "csv") != source:
            continue
        if meta.get("date") != date:
            continue
        if (meta.get("currency") or "").upper() != cur_upper:
            continue
        amt = meta.get("total_amount")
        if amt is None:
            continue
        try:
            # Round both to 2dp first — receipt amounts are inherently 2dp,
            # and this absorbs binary-float artefacts like 42.56-42.55 =
            # 0.0100000000000051 that would otherwise fail a `<= 0.01` test.
            diff = abs(round(float(amt), 2) - round(float(amount), 2))
            if diff <= tolerance + 1e-9:
                return dict(row)
        except (TypeError, ValueError):
            continue
    return None


def update_receipt_file(receipt_id: int, filename: str, filepath: str) -> bool:
    """Update where a receipt's underlying file lives. Used when we render a
    PDF for an existing Gmail-imported row that didn't have one before."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE receipts SET filename = ?, filepath = ? WHERE id = ?",
        (filename, filepath, receipt_id),
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def update_receipt_match_status(receipt_id: int, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE receipts SET match_status = ? WHERE id = ?", (status, receipt_id))
    conn.commit()
    conn.close()


def update_receipt_expense_type(receipt_id: int, expense_type: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE receipts SET expense_type = ? WHERE id = ?", (expense_type, receipt_id))
    conn.commit()
    conn.close()


def update_receipt(receipt_id: int, column_patch: dict, metadata_patch: dict) -> bool:
    """
    Apply a partial edit to a receipt. `column_patch` updates top-level columns
    (description, expense_type); `metadata_patch` is merged into the metadata
    JSON. Returns False if the receipt id doesn't exist.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT metadata FROM receipts WHERE id = ?", (receipt_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return False

    if metadata_patch:
        meta = json.loads(row["metadata"] or "{}")
        # Drop keys explicitly set to None so the caller can clear a field
        for k, v in metadata_patch.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = v
        column_patch = {**column_patch, "metadata": json.dumps(meta)}

    if not column_patch:
        conn.close()
        return True

    # Whitelist the columns we allow editing through this path
    allowed = {"metadata", "description", "expense_type"}
    safe = {k: v for k, v in column_patch.items() if k in allowed}
    if not safe:
        conn.close()
        return True

    sets = ", ".join(f"{k} = ?" for k in safe)
    cursor.execute(
        f"UPDATE receipts SET {sets} WHERE id = ?",
        (*safe.values(), receipt_id),
    )
    conn.commit()
    conn.close()
    return True


def delete_receipt(receipt_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM matches WHERE receipt_id = ?", (receipt_id,))
    cursor.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()
    conn.close()


# Statements


def save_statement(filename: str, filepath: str, transactions: list) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO statements (filename, filepath, transactions) VALUES (?, ?, ?)",
        (filename, filepath, json.dumps(transactions)),
    )
    statement_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return statement_id


def get_all_statements() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM statements ORDER BY uploaded_at DESC")
    statements = []
    for row in cursor.fetchall():
        statement = dict(row)
        statement["transactions"] = (
            json.loads(statement["transactions"]) if statement.get("transactions") else []
        )
        statements.append(statement)
    conn.close()
    return statements


# NOTE: get_statement_by_id lives further down (the JSONDecodeError-tolerant
# version). A second identical-name definition here used to shadow-fight it.


def delete_statement(statement_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    # Deleting the matches must also release their receipts — otherwise they
    # stay 'matched' with no match row and vanish from the default (active)
    # receipts view.
    cursor.execute(
        "SELECT DISTINCT receipt_id FROM matches WHERE statement_id = ?", (statement_id,)
    )
    receipt_ids = [r[0] for r in cursor.fetchall()]
    cursor.execute("DELETE FROM matches WHERE statement_id = ?", (statement_id,))
    for rid in receipt_ids:
        cursor.execute("SELECT COUNT(*) FROM matches WHERE receipt_id = ?", (rid,))
        if cursor.fetchone()[0] == 0:  # no matches left on other statements
            cursor.execute("UPDATE receipts SET match_status = 'pending' WHERE id = ?", (rid,))
    cursor.execute("DELETE FROM statements WHERE id = ?", (statement_id,))
    conn.commit()
    conn.close()


def set_statement_archived(statement_id: int, archived: bool) -> bool:
    """Mark a statement complete (archived=1) or reopen it (0). Returns False
    if no such statement."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE statements SET archived = ? WHERE id = ?",
        (1 if archived else 0, statement_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


# Matches


def save_match(
    receipt_id: int,
    statement_id: int,
    transaction_data: dict,
    match_score: float,
    *,
    match_kind: str = "auto",
    transaction_index: Optional[int] = None,
) -> int:
    """Insert a match row. Returns the new match id.

    `match_kind` is 'auto' (engine) or 'manual' (drag-drop).
    `transaction_index` pins this row to a specific parsed transaction
    inside the statement's `transactions` JSON — the drag-drop board needs
    it so the same merchant appearing twice in a month can be paired
    independently.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO matches (receipt_id, statement_id, transaction_data, "
        "match_score, match_kind, transaction_index) VALUES (?, ?, ?, ?, ?, ?)",
        (
            receipt_id,
            statement_id,
            json.dumps(transaction_data),
            match_score,
            match_kind,
            transaction_index,
        ),
    )
    match_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return match_id


def get_match_by_id(match_id: int) -> Optional[dict]:
    """Look up a single match row. Used by DELETE /api/matches/{id} so we
    can flip the linked receipt's status back to 'pending'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    m = dict(row)
    if m.get("transaction_data"):
        try:
            m["transaction_data"] = json.loads(m["transaction_data"])
        except json.JSONDecodeError:
            m["transaction_data"] = {}
    return m


def delete_match_by_id(match_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM matches WHERE id = ?", (match_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_matches_for_statement(statement_id: int) -> list:
    """Return all matches tied to a single statement, with the receipt's
    metadata joined in so the reconcile board can show vendor/amount."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT m.*, r.filename as receipt_filename, r.metadata as receipt_metadata,
               r.description as receipt_description
        FROM matches m
        JOIN receipts r ON m.receipt_id = r.id
        WHERE m.statement_id = ?
        """,
        (statement_id,),
    )
    out = []
    for row in cursor.fetchall():
        m = dict(row)
        if m.get("transaction_data"):
            try:
                m["transaction_data"] = json.loads(m["transaction_data"])
            except json.JSONDecodeError:
                m["transaction_data"] = {}
        out.append(m)
    conn.close()
    return out


def replace_statement_transactions(statement_id: int, transactions: list) -> None:
    """Overwrite the parsed transactions on a statement (used by re-parse)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE statements SET transactions = ? WHERE id = ?",
        (json.dumps(transactions), statement_id),
    )
    conn.commit()
    conn.close()


def get_statement_by_id(statement_id: int) -> Optional[dict]:
    """Return one statement row (used by /api/statements/{id}/transactions)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM statements WHERE id = ?", (statement_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    s = dict(row)
    if s.get("transactions"):
        try:
            s["transactions"] = json.loads(s["transactions"])
        except json.JSONDecodeError:
            s["transactions"] = []
    else:
        s["transactions"] = []
    return s


def clear_matches_for_statement(statement_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM matches WHERE statement_id = ?", (statement_id,))
    conn.commit()
    conn.close()


def clear_auto_matches_for_statement(statement_id: int):
    """Delete only auto-generated matches for a statement, preserving
    manual drag-drop pairings. NULL match_kind is treated as auto — that's
    the legacy case from before match_kind was a column."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM matches WHERE statement_id = ? "
        "AND (match_kind IS NULL OR match_kind = 'auto')",
        (statement_id,),
    )
    conn.commit()
    conn.close()


def get_all_matches() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.*, r.filename as receipt_filename, r.metadata as receipt_metadata,
               s.filename as statement_filename
        FROM matches m
        JOIN receipts r ON m.receipt_id = r.id
        JOIN statements s ON m.statement_id = s.id
        ORDER BY m.matched_at DESC
    """)
    matches = []
    for row in cursor.fetchall():
        m = dict(row)
        m["transaction_data"] = (
            json.loads(m["transaction_data"]) if m.get("transaction_data") else {}
        )
        matches.append(m)
    conn.close()
    return matches


# Expense reports


def save_expense_report(
    name: str,
    report_type: str,
    target_currency: str,
    period_start: Optional[str],
    period_end: Optional[str],
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO expense_reports (name, report_type, target_currency, period_start, period_end)
           VALUES (?, ?, ?, ?, ?)""",
        (name, report_type, target_currency, period_start, period_end),
    )
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return report_id


def get_expense_reports() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM expense_reports ORDER BY generated_at DESC")
    reports = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return reports


# FX rates


def cache_fx_rate(date: str, from_currency: str, to_currency: str, rate: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate)
           VALUES (?, ?, ?, ?)""",
        (date, from_currency.upper(), to_currency.upper(), rate),
    )
    conn.commit()
    conn.close()


def get_cached_rate(date: str, from_currency: str, to_currency: str) -> Optional[float]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rate FROM fx_rates WHERE date = ? AND from_currency = ? AND to_currency = ?",
        (date, from_currency.upper(), to_currency.upper()),
    )
    row = cursor.fetchone()
    conn.close()
    return row["rate"] if row else None


# Statistics


def get_statistics() -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as count FROM receipts")
    total_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM receipts WHERE match_status = 'matched'")
    matched_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM receipts WHERE match_status = 'unmatched'")
    unmatched_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM receipts WHERE match_status = 'pending'")
    pending_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM receipts WHERE expense_type = 'corp_card'")
    corp_card_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM receipts WHERE expense_type = 'reimbursement'")
    reimbursement_receipts = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM statements")
    total_statements = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM matches")
    total_matches = cursor.fetchone()["count"]

    cursor.execute("SELECT metadata FROM receipts")
    total_amount = 0.0
    for row in cursor.fetchall():
        meta = json.loads(row["metadata"] or "{}")
        amt = meta.get("total_amount")
        if amt:
            total_amount += float(amt)

    conn.close()
    return {
        "total_receipts": total_receipts,
        "matched_receipts": matched_receipts,
        "unmatched_receipts": unmatched_receipts,
        "pending_receipts": pending_receipts,
        "corp_card_receipts": corp_card_receipts,
        "reimbursement_receipts": reimbursement_receipts,
        "total_statements": total_statements,
        "total_matches": total_matches,
        "total_receipt_amount": total_amount,
    }
