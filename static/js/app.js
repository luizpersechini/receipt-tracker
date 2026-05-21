// ─── helpers ─────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const api = async (method, path, body) => {
  const opts = { method };
  if (body instanceof FormData) opts.body = body;
  else if (body) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    // FastAPI's `detail` can be a string or a structured object. Stringify
    // objects so callers that JSON.parse(e.message) can extract fields.
    const detail = err.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : res.statusText;
    throw new Error(msg);
  }
  return res.json();
};

const fmt = {
  currency: (v, cur) => {
    if (v == null) return "—";
    const sym =
      cur === "BRL" ? "R$" : cur === "EUR" ? "€" : cur === "GBP" ? "£" : "$";
    return sym + parseFloat(v).toFixed(2);
  },
  date: (v) => {
    if (!v) return "—";
    // Try to parse common formats and display nicely
    const d = new Date(v.replace(/(\d{2})\/(\d{2})\/(\d{4})/, "$3-$2-$1"));
    if (!isNaN(d))
      return d.toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      });
    return v;
  },
  month: (v) => {
    if (!v || v === "all") return "All time";
    const [y, m] = v.split("-");
    return new Date(+y, +m - 1, 1).toLocaleString("default", {
      month: "long",
      year: "numeric",
    });
  },
  file: (v) => (v ? v.replace(/^(receipt|statement)_\d{8}_\d{6}_/, "") : "—"),
  score: (v) => (v != null ? Math.round(v * 100) + "%" : "—"),
};

function statusBadge(status) {
  const map = {
    matched: ["badge-green", "Matched"],
    unmatched: ["badge-red", "Unmatched"],
    pending: ["badge-gray", "Pending"],
  };
  const [cls, label] = map[status] || ["badge-gray", status || "Pending"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function scoreBar(score) {
  const pct = Math.round((score || 0) * 100);
  const color =
    pct >= 80 ? "var(--green)" : pct >= 60 ? "var(--amber)" : "var(--red)";
  return `<div class="score-wrap">
    <div class="score-bar"><div class="score-fill" style="width:${pct}%;background:${color}"></div></div>
    <span class="score-txt">${pct}%</span>
  </div>`;
}

const categoryIcons = {
  meals: "🍽",
  travel: "✈️",
  transportation: "🚗",
  lodging: "🏨",
  office_supplies: "📎",
  other: "📌",
};

function catBadge(cat) {
  if (!cat) return "—";
  const label = cat.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return `<span class="badge badge-accent">${label}</span>`;
}

// ─── Ollama status indicator ──────────────────────────────────────────────────
async function checkOllama() {
  const dot = $("ollamaIndicator");
  const lbl = $("ollamaLabel");
  try {
    const data = await api("GET", "/api/stats");
    dot.style.background = "var(--green)";
    lbl.textContent = "Ollama · connected";
  } catch {
    dot.style.background = "var(--red)";
    lbl.textContent = "Ollama · offline";
  }
}

// ─── Month selector ───────────────────────────────────────────────────────────
let currentMonth = "all";
let allReceipts = [];

function populateMonths(receipts) {
  const months = new Set();
  receipts.forEach((r) => {
    const meta = JSON.parse(r.metadata || "{}");
    const d = meta.date || (r.created_at || "").slice(0, 7);
    if (d && d.length >= 7) months.add(d.slice(0, 7));
  });
  const sel = $("monthSelect");
  const existing = new Set([...sel.options].map((o) => o.value));
  [...months]
    .sort()
    .reverse()
    .forEach((m) => {
      if (!existing.has(m)) {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = fmt.month(m);
        sel.insertBefore(opt, sel.options[1]);
      }
    });
}

$("monthSelect").addEventListener("change", (e) => {
  currentMonth = e.target.value;
  renderReceipts(allReceipts);
  updateProgress(allReceipts);
});

function filterByMonth(receipts) {
  if (currentMonth === "all") return receipts;
  return receipts.filter((r) => {
    const meta = JSON.parse(r.metadata || "{}");
    const d = meta.date || (r.created_at || "").slice(0, 7);
    return d && d.slice(0, 7) === currentMonth;
  });
}

// ─── Progress strip ───────────────────────────────────────────────────────────
function updateProgress(receipts) {
  const filtered = filterByMonth(receipts);
  const strip = $("progressStrip");
  if (!filtered.length) {
    strip.classList.remove("visible");
    return;
  }

  const matched = filtered.filter((r) => r.match_status === "matched").length;
  const unmatched = filtered.filter(
    (r) => r.match_status === "unmatched",
  ).length;
  const pending = filtered.filter((r) => r.match_status === "pending").length;
  const total = filtered.length;
  const pct = total > 0 ? Math.round((matched / total) * 100) : 0;

  strip.classList.add("visible");
  $("progressMonth").textContent = fmt.month(currentMonth);
  $("progressFill").style.width = pct + "%";
  $("progressText").textContent = `${matched} / ${total} matched`;
  $("progressChips").innerHTML = [
    matched ? `<span class="pchip pchip-green">${matched} matched</span>` : "",
    unmatched
      ? `<span class="pchip pchip-red">${unmatched} unmatched</span>`
      : "",
    pending ? `<span class="pchip pchip-gray">${pending} pending</span>` : "",
  ].join("");
}

// ─── Stats ────────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await api("GET", "/api/stats");
    $("hPending").textContent = s.pending_receipts ?? 0;
    $("hMatched").textContent = s.matched_receipts ?? 0;
    $("hUnmatched").textContent = s.unmatched_receipts ?? 0;
    $("hAmount").textContent = fmt.currency(s.total_receipt_amount, "BRL");
  } catch {}
}

// ─── Bulk-selection state ────────────────────────────────────────────────────
const selectedIds = new Set();

function updateBulkBar() {
  const bar = $("bulkBar");
  // Drop selection ids that no longer reference visible rows — protects
  // against stale state across re-renders, tab switches, and deletes.
  const visibleIds = new Set(filterByMonth(allReceipts).map((r) => r.id));
  for (const id of [...selectedIds]) {
    if (!visibleIds.has(id)) selectedIds.delete(id);
  }
  const count = selectedIds.size;
  // Toggle inline display rather than a class so this works even if the
  // stylesheet is stale or missing the .bulk-bar rules.
  if (count === 0) {
    bar.classList.remove("visible");
    bar.style.display = "none";
  } else {
    bar.classList.add("visible");
    bar.style.display = "flex";
    $("bulkCount").textContent = `${count} selected`;
  }
  // Sync the header checkbox with current selection state
  const head = $("selectAllReceipts");
  if (head) {
    const ids = [...visibleIds];
    const allSelected =
      ids.length > 0 && ids.every((id) => selectedIds.has(id));
    head.checked = allSelected;
    head.indeterminate = !allSelected && ids.some((id) => selectedIds.has(id));
  }
}

function toggleReceiptSelection(id, checked) {
  if (checked) selectedIds.add(id);
  else selectedIds.delete(id);
  updateBulkBar();
}

function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll(".row-select").forEach((cb) => {
    cb.checked = false;
  });
  updateBulkBar();
}

// ─── Receipts ─────────────────────────────────────────────────────────────────
function renderReceipts(receipts) {
  const filtered = filterByMonth(receipts);
  const tbody = $("receiptsTbody");
  $("navReceiptCount").textContent = filtered.length;

  if (!filtered.length) {
    const msg =
      currentMonth === "all" ? "No receipts yet" : "No receipts for this month";
    tbody.innerHTML = `<tr class="empty"><td colspan="13">
      <div class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        <h3>${msg}</h3>
        <p>Upload a receipt image or PDF to get started.</p>
      </div>
    </td></tr>`;
    updateBulkBar();
    return;
  }

  tbody.innerHTML = filtered
    .map((r) => {
      const meta = JSON.parse(r.metadata || "{}");
      const hasErr = !!meta.error;
      const ext = (r.filename || "").split(".").pop().toLowerCase();
      const browserViewable = ["jpg", "jpeg", "png", "gif", "webp"].includes(
        ext,
      );
      // Uber-imported rides have no physical image; render a car icon instead
      // of a document placeholder, and skip the lightbox click.
      const isUberRow = (r.filename || "").startsWith("uber_");
      const isPdf = (r.filename || "").toLowerCase().endsWith(".pdf");
      const thumbSrc = browserViewable
        ? `/receipts/${r.filename}`
        : `/api/preview/${r.filename}`;
      const docThumb = `<div class="thumb-ph" onclick="openLightbox('${thumbSrc}', ${r.id})">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
    </div>`;
      const uberThumb = `<div class="thumb-ph" title="Imported from Uber trip-history CSV" style="cursor:default">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="17" r="2"/><circle cx="17" cy="17" r="2"/><path d="M5 17H3v-6l2-5h11l4 5v6h-2"/><path d="M5 11h14"/></svg>
    </div>`;
      // PDF (rendered Gmail receipt) — open in a new tab via the preview route,
      // which returns the file with media_type=application/pdf so the browser
      // displays it natively.
      const pdfThumb = `<a class="thumb-ph" href="/api/preview/${encodeURIComponent(r.filename)}" target="_blank" rel="noopener" title="Open Uber email receipt PDF" style="cursor:pointer;text-decoration:none;color:var(--accent)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><text x="7.5" y="18" font-size="6" font-weight="700" fill="currentColor" stroke="none">PDF</text></svg>
    </a>`;
      // Bind error handler via a named function — embedding HTML with quotes
      // inside the onerror attribute breaks attribute parsing and leaks
      // fragments into the DOM.
      const thumbEl = isPdf
        ? pdfThumb
        : isUberRow
          ? uberThumb
          : browserViewable
            ? `<img class="receipt-thumb" src="/receipts/${r.filename}" alt="receipt" data-fallback-src="${thumbSrc}" data-receipt-id="${r.id}" onclick="openLightbox('${thumbSrc}', ${r.id})" onerror="handleThumbError(this)">`
            : docThumb;

      const loc =
        [meta.city, meta.state, meta.country].filter(Boolean).join(", ") || "—";
      const vendor =
        meta.vendor_name ||
        (hasErr
          ? '<span style="color:var(--text-3);font-style:italic">OCR failed</span>'
          : "—");

      const isSelected = selectedIds.has(r.id);
      return `<tr class="data-row">
      <td style="text-align:center"><input type="checkbox" class="row-select" data-id="${r.id}" ${isSelected ? "checked" : ""}></td>
      <td style="padding:6px 10px">${thumbEl}</td>
      <td style="font-variant-numeric:tabular-nums;white-space:nowrap">${fmt.date(meta.date)}</td>
      <td style="font-weight:500">${vendor}</td>
      <td style="font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap">${fmt.currency(meta.total_amount, meta.currency)}</td>
      <td>${meta.currency ? `<span class="badge badge-gray mono">${meta.currency}</span>` : "—"}</td>
      <td>${meta.country ? `<span class="badge badge-gray mono">${meta.country}</span>` : '<span style="color:var(--text-3)">—</span>'}</td>
      <td style="color:var(--text-2);white-space:nowrap">${loc}</td>
      <td>${catBadge(meta.category)}</td>
      <td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-2)">${r.description || '<span style="color:var(--text-3)">—</span>'}</td>
      <td>${statusBadge(r.match_status || "pending")}</td>
      <td><button class="type-toggle ${r.expense_type || "corp_card"}" onclick="toggleExpenseType(${r.id}, '${r.expense_type || "corp_card"}')">${r.expense_type === "reimbursement" ? "Reimburse" : "Corp Card"}</button></td>
      <td style="white-space:nowrap">
        <button class="btn btn-outline btn-sm" onclick="openEditModalById(${r.id})">Edit</button>
        <button class="btn btn-ghost btn-sm" onclick="deleteReceipt(${r.id})">Del</button>
      </td>
    </tr>`;
    })
    .join("");
  updateBulkBar();
}

// Delegated change handler — fires for any row checkbox + the header
// "select all" without re-binding listeners on every re-render.
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains("row-select")) {
    toggleReceiptSelection(parseInt(t.dataset.id, 10), t.checked);
  } else if (t && t.id === "selectAllReceipts") {
    const visible = filterByMonth(allReceipts);
    if (t.checked) {
      visible.forEach((r) => selectedIds.add(r.id));
    } else {
      visible.forEach((r) => selectedIds.delete(r.id));
    }
    document.querySelectorAll(".row-select").forEach((cb) => {
      cb.checked = selectedIds.has(parseInt(cb.dataset.id, 10));
    });
    updateBulkBar();
  }
});

$("bulkClearBtn").addEventListener("click", clearSelection);

$("bulkDeleteBtn").addEventListener("click", async () => {
  const count = selectedIds.size;
  if (!count) return;
  if (
    !confirm(
      `Delete ${count} receipt${count !== 1 ? "s" : ""}? This can't be undone.`,
    )
  )
    return;
  const ids = [...selectedIds];
  const btn = $("bulkDeleteBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Deleting…';
  try {
    await api("POST", "/api/receipts/bulk-delete", { ids });
    clearSelection();
    await loadReceipts();
    await loadStats();
  } catch (e) {
    alert("Delete failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg> Delete selected`;
  }
});

async function loadReceipts() {
  try {
    const { receipts } = await api("GET", "/api/receipts");
    allReceipts = receipts;
    populateMonths(receipts);
    renderReceipts(receipts);
    updateProgress(receipts);
  } catch {}
}

async function deleteReceipt(id) {
  if (!confirm("Remove this receipt?")) return;
  await api("DELETE", `/api/receipts/${id}`);
  allReceipts = allReceipts.filter((r) => r.id !== id);
  renderReceipts(allReceipts);
  updateProgress(allReceipts);
  loadStats();
}

// ─── Statements ───────────────────────────────────────────────────────────────
async function loadStatements() {
  let statements = [];
  try {
    ({ statements } = await api("GET", "/api/statements"));
  } catch {}
  $("navStmtCount").textContent = statements.length;
  const tbody = $("statementsTbody");
  if (!statements.length) {
    tbody.innerHTML = `<tr class="empty"><td colspan="5">
      <div class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <h3>No statements yet</h3><p>Upload a corporate card CSV to begin reconciliation.</p>
      </div>
    </td></tr>`;
    return;
  }
  tbody.innerHTML = statements
    .map(
      (s) => `<tr class="data-row">
    <td style="color:var(--text-3);font-family:'DM Mono',monospace;font-size:12px">#${s.id}</td>
    <td class="fname" title="${s.filename}" style="font-weight:500">${fmt.file(s.filename)}</td>
    <td><span class="badge badge-gray">${(s.transactions || []).length} txns</span></td>
    <td style="color:var(--text-2);font-size:12px">${(s.uploaded_at || "").slice(0, 16).replace("T", " ")}</td>
    <td><button class="btn btn-ghost btn-sm" onclick="deleteStatement(${s.id})">Del</button></td>
  </tr>`,
    )
    .join("");
}

async function deleteStatement(id) {
  if (!confirm("Remove this statement and its match results?")) return;
  await api("DELETE", `/api/statements/${id}`);
  loadStatements();
  loadReceipts();
  loadStats();
  $("reconcileContent").innerHTML =
    '<div class="alert alert-gray">Statement removed. Upload a new one to reconcile.</div>';
}

// ─── Reconcile ────────────────────────────────────────────────────────────────
function renderReconcile(result) {
  const { matches, unmatched_receipts, unmatched_transactions } = result;
  let html = "";

  // Summary
  if (!unmatched_receipts.length && !unmatched_transactions.length) {
    html += `<div class="alert alert-green">✓ All <strong>${matches.length}</strong> receipts matched to statement transactions.</div>`;
  } else {
    html += `<div class="alert alert-amber">
      <strong>${matches.length}</strong> matched · <strong>${unmatched_receipts.length}</strong> receipt${unmatched_receipts.length !== 1 ? "s" : ""} without a transaction · <strong>${unmatched_transactions.length}</strong> statement transaction${unmatched_transactions.length !== 1 ? "s" : ""} without a receipt
    </div>`;
  }

  // Matched pairs
  if (matches.length) {
    html += `<div class="rec-section">
      <div class="rec-section-head">
        <span class="rec-section-label">Matched Pairs</span>
        <span class="rec-section-count">${matches.length}</span>
      </div>
      <div class="card" style="padding:0;overflow:hidden">
        <div class="table-wrap"><table>
          <thead><tr>
            <th>Receipt</th><th>Date</th><th>Vendor</th><th>Amount</th>
            <th>→ Transaction</th><th>Txn Date</th><th>Txn Amount</th><th>Score</th>
          </tr></thead>
          <tbody>
            ${matches
              .map(
                (m) => `<tr class="data-row">
              <td class="fname" title="${m.receipt_filename}" style="font-weight:500">${fmt.file(m.receipt_filename)}</td>
              <td style="white-space:nowrap">${fmt.date(m.receipt_date)}</td>
              <td>${m.receipt_vendor || "—"}</td>
              <td style="font-weight:600">${fmt.currency(m.receipt_amount, "BRL")}</td>
              <td style="color:var(--text-2)">${m.transaction_description || "—"}</td>
              <td style="white-space:nowrap">${fmt.date(m.transaction_date)}</td>
              <td style="font-weight:600">${fmt.currency(m.transaction_amount, "BRL")}</td>
              <td>${scoreBar(m.match_score)}</td>
            </tr>`,
              )
              .join("")}
          </tbody>
        </table></div>
      </div>
    </div>`;
  }

  // Unmatched receipts
  if (unmatched_receipts.length) {
    html += `<div class="rec-section">
      <div class="rec-section-head">
        <span class="rec-section-label" style="color:var(--red)">Receipts Without a Match</span>
        <span class="rec-section-count" style="color:var(--red)">${unmatched_receipts.length} — needs attention</span>
      </div>
      <div class="card" style="padding:0;overflow:hidden;border-color:var(--red-border)">
        <div class="table-wrap"><table>
          <thead><tr style="background:var(--red-bg)"><th>Receipt</th><th>Date</th><th>Vendor</th><th>Amount</th><th>Description</th></tr></thead>
          <tbody>
            ${unmatched_receipts
              .map((r) => {
                const meta = JSON.parse(r.metadata || "{}");
                return `<tr class="data-row">
                <td class="fname" title="${r.filename}" style="font-weight:500">${fmt.file(r.filename)}</td>
                <td style="white-space:nowrap">${fmt.date(meta.date)}</td>
                <td>${meta.vendor_name || "—"}</td>
                <td style="font-weight:600">${fmt.currency(meta.total_amount, meta.currency)}</td>
                <td style="color:var(--text-2)">${r.description || "—"}</td>
              </tr>`;
              })
              .join("")}
          </tbody>
        </table></div>
      </div>
    </div>`;
  }

  // Unmatched transactions
  if (unmatched_transactions.length) {
    html += `<div class="rec-section">
      <div class="rec-section-head">
        <span class="rec-section-label" style="color:var(--amber)">Transactions Without a Receipt</span>
        <span class="rec-section-count">${unmatched_transactions.length}</span>
      </div>
      <div class="card" style="padding:0;overflow:hidden;border-color:var(--amber-border)">
        <div class="table-wrap"><table>
          <thead><tr style="background:var(--amber-bg)"><th>Description</th><th>Date</th><th>Amount</th><th>Category</th></tr></thead>
          <tbody>
            ${unmatched_transactions
              .map((ut) => {
                const t = ut.transaction;
                return `<tr class="data-row">
                <td style="font-weight:500">${t.description || "—"}</td>
                <td style="white-space:nowrap">${fmt.date(t.date)}</td>
                <td style="font-weight:600">${fmt.currency(t.amount, "BRL")}</td>
                <td>${t.category ? `<span class="badge badge-gray">${t.category}</span>` : "—"}</td>
              </tr>`;
              })
              .join("")}
          </tbody>
        </table></div>
      </div>
    </div>`;
  }

  $("reconcileContent").innerHTML = html;
}

$("matchBtn").addEventListener("click", async () => {
  const btn = $("matchBtn");
  const msg = $("matchMsg");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running…';
  msg.textContent = "";
  try {
    const res = await api("POST", "/api/match");
    if (res.success) {
      renderReconcile(res);
      msg.innerHTML = `<strong>${res.total_matched}</strong> matched · <strong>${res.total_unmatched_receipts}</strong> unmatched`;
      loadReceipts();
      loadStats();
    } else {
      msg.textContent = res.message;
    }
  } catch (e) {
    msg.textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> Run Matching`;
  }
});

// ─── Expense type toggle ──────────────────────────────────────────────────────
async function toggleExpenseType(id, current) {
  const next = current === "reimbursement" ? "corp_card" : "reimbursement";
  try {
    await api("PATCH", `/api/receipts/${id}/expense-type`, {
      expense_type: next,
    });
    allReceipts = allReceipts.map((r) =>
      r.id === id ? { ...r, expense_type: next } : r,
    );
    renderReceipts(allReceipts);
  } catch (e) {
    alert("Could not update: " + e.message);
  }
}

// ─── FX Rates ─────────────────────────────────────────────────────────────────
let fxRatesData = {};

async function loadFxRates() {
  const sel = $("reportCurrency");
  const line = $("fxRatesLine");
  try {
    const data = await api("GET", "/api/fx/rates?base=BRL");
    fxRatesData = data.rates || {};
    const currencies = Object.keys(fxRatesData).sort();
    const current = sel.value;
    sel.innerHTML = "";
    ["BRL", ...currencies.filter((c) => c !== "BRL")].forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      if (c === current) opt.selected = true;
      sel.appendChild(opt);
    });
    const preview = currencies
      .filter((c) => c !== "BRL" && fxRatesData[c] != null)
      .slice(0, 3)
      .map((c) => `<strong>1 ${c} = ${fxRatesData[c].toFixed(4)} BRL</strong>`)
      .join(" &nbsp;·&nbsp; ");
    line.innerHTML = preview ? `Today: ${preview}` : "";
  } catch {
    line.textContent = "Rates unavailable";
  }
}

// ─── Report button ────────────────────────────────────────────────────────────
$("reportBtn").addEventListener("click", async () => {
  const btn = $("reportBtn");
  const type = $("reportType").value;
  const cur = $("reportCurrency").value || "BRL";
  const from = $("reportFrom").value || null;
  const to = $("reportTo").value || null;

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Generating…';
  try {
    const res = await fetch("/api/reports/expense", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        report_type: type,
        target_currency: cur,
        period_start: from,
        period_end: to,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `expense_report_${type}_${cur}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("Report error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Export Report`;
  }
});

// ─── Folder scan ──────────────────────────────────────────────────────────────
async function loadWatchFolder() {
  try {
    const data = await api("GET", "/api/watch-folder/status");
    if (!data.enabled) return;
    $("watchFolderCard").style.display = "";
    $("watchFolderPath").textContent = data.folder || "";
    $("watchFolderPath").title = data.folder || "";
    const pending = data.pending || 0;
    const badge = $("watchPendingBadge");
    badge.textContent = `${pending} pending`;
    badge.className = "badge " + (pending > 0 ? "badge-amber" : "badge-gray");
    const entries = data.last_processed || [];
    $("watchLogList").innerHTML = entries.length
      ? entries
          .map((e) => {
            const ok = e.status === "ok";
            const ts = (e.timestamp || "").slice(11, 16);
            const n = e.count && e.count > 1 ? ` · ${e.count}` : "";
            return `<div class="wl-row">
            <div class="wl-dot ${ok ? "ok" : "err"}"></div>
            <span class="wl-name" title="${e.source || ""}">${e.filename || ""}${n}</span>
            <span class="wl-meta">${ts}</span>
          </div>`;
          })
          .join("")
      : '<div style="font-size:12px;color:var(--text-3)">No files imported yet</div>';
  } catch {}
}

$("scanFolderBtn").addEventListener("click", async () => {
  const btn = $("scanFolderBtn");
  const status = $("scanStatus");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Scanning…';
  status.textContent = "Scanning folder — OCR can take a few seconds per file…";
  status.className = "status-msg load";
  try {
    const res = await api("POST", "/api/watch-folder/scan");
    if (res.scanned === 0) {
      status.textContent =
        "Folder is empty — drop some receipts in and scan again.";
      status.className = "status-msg";
    } else {
      const bits = [
        `${res.imported_receipts} receipt${res.imported_receipts === 1 ? "" : "s"} from ${res.files_ok} file${res.files_ok === 1 ? "" : "s"}`,
      ];
      if (res.files_failed) bits.push(`${res.files_failed} failed`);
      status.textContent = bits.join(" · ");
      status.className = "status-msg " + (res.files_failed ? "err" : "ok");
    }
    loadReceipts();
    loadStats();
    loadWatchFolder();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
});

// ─── AI Processing Modal ──────────────────────────────────────────────────────
function setAiStep(idx, state, detail) {
  // state: 'idle' | 'active' | 'done' | 'error'
  for (let i = 0; i < 3; i++) {
    const el = $(`aiStep${i}`);
    el.classList.remove("active", "done", "error");
  }
  for (let i = 0; i < idx; i++) $(`aiStep${i}`).classList.add("done");
  if (state !== "idle") $(`aiStep${idx}`).classList.add(state);
  if (detail !== undefined) $(`aiStep${idx}Detail`).textContent = detail;

  // Update icon spinner for active
  const icon = $(`aiStep${idx}`).querySelector(".ai-step-icon");
  if (state === "active") {
    icon.innerHTML = `<span class="spin" style="width:12px;height:12px;border-width:2px"></span>`;
  } else if (state === "done") {
    icon.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
  } else if (state === "error") {
    icon.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
  }
}

function openAiModal(filename) {
  $("aiModal").classList.add("open");
  $("aiModalTitle").textContent = "Processing Receipt";
  $("aiModalSub").textContent = filename;
  $("aiResult").classList.remove("visible");
  // Reset steps
  for (let i = 0; i < 3; i++) {
    const el = $(`aiStep${i}`);
    el.classList.remove("active", "done", "error");
    const icons = [
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>`,
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>`,
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>`,
    ];
    el.querySelector(".ai-step-icon").innerHTML = icons[i];
    $(`aiStep${i}Detail`).textContent =
      i === 0 ? "Preparing file…" : "Waiting…";
  }
}

function closeAiModal() {
  $("aiModal").classList.remove("open");
}

function showAiResult(metas) {
  // Accept either a single metadata dict or an array of them.
  const arr = Array.isArray(metas) ? metas : [metas];
  if (arr.length === 1) {
    const meta = arr[0] || {};
    const cur = meta.currency || "";
    $("arVendor").textContent = meta.vendor_name || "—";
    $("arDate").textContent = fmt.date(meta.date);
    $("arAmount").textContent =
      fmt.currency(meta.total_amount, cur) + (cur ? ` ${cur}` : "");
    $("arCat").textContent = (meta.category || "other")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  } else {
    // Multi-receipt: show first row's vendor with a count badge, then the
    // total amount summed across receipts (in their own currencies for
    // simplicity — no FX here).
    $("arVendor").textContent = `${arr.length} receipts in this image`;
    $("arDate").textContent =
      arr
        .map((m) => fmt.date(m.date))
        .filter((d) => d && d !== "—")
        .slice(0, 3)
        .join(" · ") || "—";
    const byCur = {};
    arr.forEach((m) => {
      const c = m.currency || "";
      byCur[c] = (byCur[c] || 0) + (parseFloat(m.total_amount) || 0);
    });
    $("arAmount").textContent = Object.entries(byCur)
      .map(([c, v]) => fmt.currency(v, c) + (c ? ` ${c}` : ""))
      .join(" + ");
    const cats = [
      ...new Set(arr.map((m) => (m.category || "other").replace(/_/g, " "))),
    ];
    $("arCat").textContent = cats
      .map((c) => c.replace(/\b\w/g, (ch) => ch.toUpperCase()))
      .join(", ");
  }
  $("aiResult").classList.add("visible");
}

// ─── BBox editor ──────────────────────────────────────────────────────────────
// Manual review of segmenter output before OCR runs. Lets the user drag,
// resize, delete, or add bounding boxes on the uploaded image, then commit
// to OCR each one as a separate receipt.

const SVG_NS = "http://www.w3.org/2000/svg";

class BBoxEditor {
  constructor({ wrap, img, svg, onChange }) {
    this.wrap = wrap; // .bbox-canvas-wrap
    this.img = img; // <img>
    this.svg = svg; // <svg>
    this.onChange = onChange || (() => {});
    this.boxes = []; // [{x,y,w,h}] in IMAGE pixel coords
    this.imageSize = { w: 1, h: 1 };
    this.selected = -1;
    this.drag = null; // current drag state
    this.drawing = false; // adding a new box?
    this._bind();
  }

  load(imageUrl, imageSize, boxes) {
    this.imageSize = imageSize;
    this.boxes = boxes.map((b) => ({ ...b }));
    this.selected = this.boxes.length ? 0 : -1;

    const finish = () => {
      this.svg.setAttribute("viewBox", `0 0 ${imageSize.w} ${imageSize.h}`);
      this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
      this.render();
      this.onChange();
    };

    // URL-encode the path so filenames with spaces (very common from phone
    // photos) don't 404. encodeURI keeps the slashes.
    const safeUrl = encodeURI(imageUrl);

    // Attach handlers BEFORE assigning .src so we don't miss a cache-hit
    // load that fires synchronously. img.complete handles the case where
    // the browser already had the resource.
    this.img.onload = finish;
    this.img.onerror = () => {
      this.svg.setAttribute("viewBox", `0 0 ${imageSize.w} ${imageSize.h}`);
      console.error("BBoxEditor: failed to load image", safeUrl);
    };
    this.img.src = safeUrl;
    if (this.img.complete && this.img.naturalWidth > 0) finish();
  }

  enableDrawing(on) {
    this.drawing = on;
    this.wrap.classList.toggle("drawing", on);
  }

  addBox(b) {
    this.boxes.push({ ...b });
    this.selected = this.boxes.length - 1;
    this.render();
    this.onChange();
  }

  removeBox(i) {
    if (i < 0 || i >= this.boxes.length) return;
    this.boxes.splice(i, 1);
    if (this.selected >= this.boxes.length)
      this.selected = this.boxes.length - 1;
    this.render();
    this.onChange();
  }

  resetBoxes(boxes) {
    this.boxes = boxes.map((b) => ({ ...b }));
    this.selected = this.boxes.length ? 0 : -1;
    this.render();
    this.onChange();
  }

  getBoxes() {
    return this.boxes.map((b) => ({ ...b }));
  }

  // Convert a mouse event's clientX/Y → SVG (image) coordinates
  _imageCoords(evt) {
    const pt = this.svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const ctm = this.svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const transformed = pt.matrixTransform(ctm.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  _clamp(b) {
    const W = this.imageSize.w,
      H = this.imageSize.h;
    let x = Math.max(0, Math.min(b.x, W - 1));
    let y = Math.max(0, Math.min(b.y, H - 1));
    let w = Math.max(1, Math.min(b.w, W - x));
    let h = Math.max(1, Math.min(b.h, H - y));
    return {
      x: Math.round(x),
      y: Math.round(y),
      w: Math.round(w),
      h: Math.round(h),
    };
  }

  render() {
    // Clear
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild);
    // Handle size scales with image so handles stay visible on big images
    const hsize = Math.max(
      8,
      Math.min(this.imageSize.w, this.imageSize.h) / 80,
    );

    this.boxes.forEach((b, i) => {
      // Main rect
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute(
        "class",
        "bbox" + (i === this.selected ? " selected" : ""),
      );
      rect.setAttribute("x", b.x);
      rect.setAttribute("y", b.y);
      rect.setAttribute("width", b.w);
      rect.setAttribute("height", b.h);
      rect.dataset.idx = i;
      rect.addEventListener("mousedown", (e) => this._startMove(e, i));
      this.svg.appendChild(rect);

      // Corner resize handles
      const corners = [
        { sx: 0, sy: 0, cx: b.x, cy: b.y }, // NW
        { sx: 1, sy: 0, cx: b.x + b.w, cy: b.y }, // NE
        { sx: 1, sy: 1, cx: b.x + b.w, cy: b.y + b.h }, // SE
        { sx: 0, sy: 1, cx: b.x, cy: b.y + b.h }, // SW
      ];
      corners.forEach((c) => {
        const h = document.createElementNS(SVG_NS, "circle");
        h.setAttribute("class", "handle");
        h.setAttribute("cx", c.cx);
        h.setAttribute("cy", c.cy);
        h.setAttribute("r", hsize / 2);
        h.style.cursor = c.sx === c.sy ? "nwse-resize" : "nesw-resize";
        h.addEventListener("mousedown", (e) =>
          this._startResize(e, i, c.sx, c.sy),
        );
        this.svg.appendChild(h);
      });

      // Delete chip in the top-right corner
      const del = document.createElementNS(SVG_NS, "g");
      del.setAttribute("class", "delete");
      const dc = document.createElementNS(SVG_NS, "circle");
      dc.setAttribute("cx", b.x + b.w - hsize);
      dc.setAttribute("cy", b.y + hsize);
      dc.setAttribute("r", hsize * 0.9);
      const dp = document.createElementNS(SVG_NS, "path");
      const cxd = b.x + b.w - hsize,
        cyd = b.y + hsize;
      const r = hsize * 0.45;
      dp.setAttribute(
        "d",
        `M${cxd - r} ${cyd - r} L${cxd + r} ${cyd + r} M${cxd - r} ${cyd + r} L${cxd + r} ${cyd - r}`,
      );
      del.appendChild(dc);
      del.appendChild(dp);
      del.addEventListener("mousedown", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.removeBox(i);
      });
      this.svg.appendChild(del);

      // Index label
      const lbl = document.createElementNS(SVG_NS, "text");
      lbl.setAttribute("x", b.x + hsize * 0.6);
      lbl.setAttribute("y", b.y + hsize * 1.4);
      lbl.setAttribute("fill", "#fff");
      lbl.setAttribute("stroke", "var(--accent)");
      lbl.setAttribute("stroke-width", "0.6");
      lbl.setAttribute("font-size", String(hsize * 1.4));
      lbl.setAttribute("font-weight", "700");
      lbl.style.pointerEvents = "none";
      lbl.textContent = String(i + 1);
      this.svg.appendChild(lbl);
    });
  }

  _bind() {
    this.svg.addEventListener("mousedown", (e) => {
      if (this.drawing) this._startDraw(e);
    });
    window.addEventListener("mousemove", (e) => {
      if (this.drag) this._onDragMove(e);
    });
    window.addEventListener("mouseup", () => {
      if (this.drag) this._endDrag();
    });
  }

  _startMove(e, i) {
    if (this.drawing) return; // drawing mode: only new-box draws fire
    e.preventDefault();
    e.stopPropagation();
    this.selected = i;
    const start = this._imageCoords(e);
    const orig = { ...this.boxes[i] };
    this.drag = { mode: "move", i, start, orig };
    this.render();
  }

  _startResize(e, i, sx, sy) {
    e.preventDefault();
    e.stopPropagation();
    this.selected = i;
    const start = this._imageCoords(e);
    const orig = { ...this.boxes[i] };
    this.drag = { mode: "resize", i, start, orig, sx, sy };
    this.render();
  }

  _startDraw(e) {
    const start = this._imageCoords(e);
    this.boxes.push({ x: start.x, y: start.y, w: 1, h: 1 });
    this.selected = this.boxes.length - 1;
    this.drag = { mode: "draw", i: this.selected, start };
    this.render();
  }

  _onDragMove(e) {
    const p = this._imageCoords(e);
    const d = this.drag;
    if (d.mode === "move") {
      const dx = p.x - d.start.x,
        dy = p.y - d.start.y;
      this.boxes[d.i] = this._clamp({
        x: d.orig.x + dx,
        y: d.orig.y + dy,
        w: d.orig.w,
        h: d.orig.h,
      });
    } else if (d.mode === "resize") {
      const dx = p.x - d.start.x,
        dy = p.y - d.start.y;
      let x = d.orig.x,
        y = d.orig.y,
        w = d.orig.w,
        h = d.orig.h;
      if (d.sx === 0) {
        x += dx;
        w -= dx;
      } else {
        w += dx;
      }
      if (d.sy === 0) {
        y += dy;
        h -= dy;
      } else {
        h += dy;
      }
      if (w < 1) {
        x += w - 1;
        w = 1;
      }
      if (h < 1) {
        y += h - 1;
        h = 1;
      }
      this.boxes[d.i] = this._clamp({ x, y, w, h });
    } else if (d.mode === "draw") {
      const x = Math.min(d.start.x, p.x);
      const y = Math.min(d.start.y, p.y);
      const w = Math.abs(p.x - d.start.x);
      const h = Math.abs(p.y - d.start.y);
      this.boxes[d.i] = this._clamp({
        x,
        y,
        w: Math.max(w, 1),
        h: Math.max(h, 1),
      });
    }
    this.render();
  }

  _endDrag() {
    if (this.drag && this.drag.mode === "draw") {
      // Reject tiny strays (< 0.5% of the smaller image dimension on each side)
      const b = this.boxes[this.drag.i];
      const minSide = Math.min(this.imageSize.w, this.imageSize.h) * 0.02;
      if (b.w < minSide || b.h < minSide) {
        this.boxes.splice(this.drag.i, 1);
        this.selected = this.boxes.length - 1;
      }
      this.enableDrawing(false);
    }
    this.drag = null;
    this.render();
    this.onChange();
  }
}

let _bboxEditor = null;
let _bboxState = null; // {filename, image_url, image_size, initialBoxes, description}

function _bboxUpdateCount() {
  const n = _bboxEditor ? _bboxEditor.getBoxes().length : 0;
  $("bboxCount").textContent = `${n} box${n === 1 ? "" : "es"}`;
  $("bboxCommitBtn").disabled = n < 1;
}

function openBBoxEditor(state) {
  _bboxState = state;
  $("bboxStatus").textContent = "";
  $("bboxStatus").className = "status-msg";
  $("bboxCommitBtn").disabled = false;
  $("bboxCommitBtn").innerHTML =
    `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Run OCR`;
  $("bboxModal").classList.add("open");
  if (!_bboxEditor) {
    _bboxEditor = new BBoxEditor({
      wrap: $("bboxCanvasWrap"),
      img: $("bboxImg"),
      svg: $("bboxSvg"),
      onChange: _bboxUpdateCount,
    });
  }
  _bboxEditor.load(state.image_url, state.image_size, state.initialBoxes);
}

function closeBBoxEditor() {
  $("bboxModal").classList.remove("open");
}

$("bboxCancelBtn").addEventListener("click", closeBBoxEditor);
$("bboxResetBtn").addEventListener("click", () => {
  if (_bboxEditor && _bboxState)
    _bboxEditor.resetBoxes(_bboxState.initialBoxes);
});
$("bboxAddBtn").addEventListener("click", () => {
  if (_bboxEditor) _bboxEditor.enableDrawing(true);
});

$("bboxCommitBtn").addEventListener("click", async () => {
  if (!_bboxEditor || !_bboxState) return;
  const boxes = _bboxEditor.getBoxes();
  if (!boxes.length) return;
  const btn = $("bboxCommitBtn");
  const status = $("bboxStatus");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running OCR…';
  status.textContent = `Running OCR on ${boxes.length} crop${boxes.length === 1 ? "" : "s"}…`;
  status.className = "status-msg load";
  try {
    const res = await api("POST", "/api/upload/receipt/commit", {
      filename: _bboxState.filename,
      description: _bboxState.description || null,
      bboxes: boxes,
    });
    status.textContent = `Saved ${res.count} receipt${res.count === 1 ? "" : "s"}`;
    status.className = "status-msg ok";
    // Mirror the summary down in the side-panel uploader so it's still
    // visible after the editor closes.
    $("receiptStatus").textContent =
      `Saved ${res.count} receipt${res.count === 1 ? "" : "s"} from this image`;
    $("receiptStatus").className = "status-msg ok";
    loadReceipts();
    loadStats();
    setTimeout(closeBBoxEditor, 600);
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Run OCR`;
  }
});

// ─── Edit Receipt Modal ───────────────────────────────────────────────────────
let editingReceiptId = null;

function openEditModalById(id) {
  const r = allReceipts.find((x) => x.id === id);
  if (r) openEditModal(r);
}

function openEditModal(r) {
  editingReceiptId = r.id;
  const meta = JSON.parse(r.metadata || "{}");
  $("editModalSub").textContent = r.filename || `Receipt #${r.id}`;

  $("editVendor").value = meta.vendor_name || "";
  $("editDate").value = _normalizeDate(meta.date);
  $("editAmount").value = meta.total_amount ?? "";
  $("editCurrency").value = (meta.currency || "").toUpperCase();
  $("editCategory").value = meta.category || "other";
  $("editCity").value = meta.city || "";
  $("editState").value = meta.state || "";
  $("editCountry").value = (meta.country || "").toUpperCase();
  $("editExpenseType").value = r.expense_type || "corp_card";
  $("editDescription").value = r.description || "";
  $("editStatus").textContent = "";
  $("editStatus").className = "status-msg";
  $("editModal").classList.add("open");
  setTimeout(() => $("editVendor").focus(), 50);
}

function closeEditModal() {
  $("editModal").classList.remove("open");
  editingReceiptId = null;
}

// `<input type="date">` requires YYYY-MM-DD. Accept anything close and
// fall back to empty if we can't parse it (the OCR sometimes returns
// raw printed-on-receipt formats like "25/04/2026").
function _normalizeDate(s) {
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const m = s.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
  if (m) return `${m[3]}-${m[2]}-${m[1]}`;
  const d = new Date(s);
  if (!isNaN(d)) return d.toISOString().slice(0, 10);
  return "";
}

$("editCancelBtn").addEventListener("click", closeEditModal);
$("editModal").addEventListener("click", (e) => {
  if (e.target === $("editModal")) closeEditModal();
});

$("editSaveBtn").addEventListener("click", async () => {
  if (editingReceiptId == null) return;
  const btn = $("editSaveBtn");
  const status = $("editStatus");

  const trim = (v) => (typeof v === "string" ? v.trim() : v);
  const orNull = (v) => (v === "" || v == null ? null : v);

  const amount = parseFloat($("editAmount").value);
  const body = {
    vendor_name: orNull(trim($("editVendor").value)),
    date: orNull($("editDate").value),
    total_amount: Number.isFinite(amount) ? amount : null,
    currency: orNull(trim($("editCurrency").value).toUpperCase()),
    category: $("editCategory").value || null,
    city: orNull(trim($("editCity").value)),
    state: orNull(trim($("editState").value)),
    country: orNull(trim($("editCountry").value).toUpperCase()),
    description: orNull(trim($("editDescription").value)),
    expense_type: $("editExpenseType").value || null,
  };

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Saving…';
  status.textContent = "";

  try {
    await api("PATCH", `/api/receipts/${editingReceiptId}`, body);
    status.textContent = "Saved";
    status.className = "status-msg ok";
    await loadReceipts();
    await loadStats();
    setTimeout(closeEditModal, 400);
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = "Save changes";
  }
});

// ─── Tabs ─────────────────────────────────────────────────────────────────────
const tabTitles = {
  receipts: "Receipts",
  reconcile: "Reconcile",
  statements: "Statements",
};
const tabPanels = {
  receipts: $("tab-receipts"),
  reconcile: $("tab-reconcile"),
  statements: $("tab-statements"),
};

document.querySelectorAll(".nav-item[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const name = btn.dataset.tab;
    document
      .querySelectorAll(".nav-item[data-tab]")
      .forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    Object.entries(tabPanels).forEach(
      ([k, el]) => (el.style.display = k === name ? "" : "none"),
    );
    $("topbarTitle").textContent = tabTitles[name];
    if (name === "receipts") loadReceipts();
    if (name === "statements") loadStatements();
  });
});

// ─── Upload receipt ───────────────────────────────────────────────────────────
// Holds the full FileList the user selected. Bulk-aware: any length ≥ 1
// flows through the same queue runner below.
let receiptFiles = [];

function _renderReceiptFilename() {
  const el = $("receiptFilename");
  if (receiptFiles.length === 0) {
    el.textContent = "";
    return;
  }
  if (receiptFiles.length === 1) {
    el.textContent = receiptFiles[0].name;
    return;
  }
  // Show "3 files: a.jpg, b.jpg, c.jpg" truncating at ~80 chars
  const names = receiptFiles.map((f) => f.name).join(", ");
  el.textContent = `${receiptFiles.length} files: ${names.length > 80 ? names.slice(0, 77) + "…" : names}`;
}

$("receiptFile").addEventListener("change", () => {
  receiptFiles = Array.from($("receiptFile").files || []);
  $("uploadReceiptBtn").disabled = receiptFiles.length === 0;
  _renderReceiptFilename();
  if (receiptFiles.length) {
    $("receiptStatus").textContent = "";
    $("receiptStatus").className = "status-msg";
  }
});

async function commitReceiptOCR({
  filename,
  bboxes,
  description,
  fname,
  bulk = false,
}) {
  // Runs the AI-modal progress UI through OCR step. Used by both the
  // "single-receipt fast path" (called straight after preview) and the
  // bbox editor's commit button.
  //
  // `bulk=true` is set when this is one of N files in a bulk upload —
  // the caller owns the outer status message and modal lifecycle, so we
  // only return a compact result and skip the result-card + status copy.
  setAiStep(1, "active", "Running vision model (Ollama)…");
  if (!bulk)
    $("uploadReceiptBtn").innerHTML = '<span class="spin"></span> OCR running…';

  const res = await api("POST", "/api/upload/receipt/commit", {
    filename,
    bboxes,
    description: description || null,
  });
  const receipts = res.receipts || [];
  const metas = receipts.map((r) => r.metadata || {});
  const ids = receipts.map((r) => r.receipt_id);
  const firstErr = metas.find((m) => m.error)?.error;
  const count = metas.length;

  setAiStep(
    1,
    "done",
    firstErr
      ? "OCR error — check model"
      : count > 1
        ? `Extracted ${count} receipts`
        : "Extracted successfully",
  );
  setAiStep(2, "active", "Saving to database…");
  if (!bulk)
    $("uploadReceiptBtn").innerHTML = '<span class="spin"></span> Saving…';
  await new Promise((r) => setTimeout(r, 200));
  setAiStep(
    2,
    "done",
    count > 1
      ? `Saved ${count} receipts (ids ${ids.join(", ")})`
      : `Saved as receipt #${ids[0] ?? "?"}`,
  );

  // In bulk mode, hand control back to the loop runner — no result card,
  // no auto-close, no status mutation here.
  if (bulk) {
    return { metas, ids, firstErr, count };
  }

  if (!firstErr && metas.length) showAiResult(metas);

  const summary =
    count > 1
      ? `Saved ${count} receipts from this image`
      : metas[0]
        ? `${metas[0].vendor_name || "unknown"}, ${fmt.currency(metas[0].total_amount, metas[0].currency)}`
        : "no result";

  $("aiModalSub").textContent = firstErr
    ? `⚠ OCR returned an error`
    : `Done — ${summary}`;
  $("receiptStatus").textContent = firstErr
    ? `Saved (OCR error)`
    : `Saved — ${summary}`;
  $("receiptStatus").className = "status-msg " + (firstErr ? "err" : "ok");

  loadReceipts();
  loadStats();
  setTimeout(closeAiModal, firstErr ? 0 : 4000);
  return { metas, ids, firstErr, count };
}

// Process ONE file through preview → (bbox editor if needed) → commit.
// Returns a result object the bulk runner can aggregate.
//   { ok: true,  count, ids, vendor, total, currency }
//   { ok: false, error: '...' }
// When `bulk` is true we never open the bbox editor — we auto-commit on
// whatever the segmenter returned, because holding the queue for per-file
// confirmation would defeat the point of bulk.
async function _processSingleReceiptFile(file, desc, { bulk = false } = {}) {
  const fd = new FormData();
  fd.append("file", file);
  const fname = file.name;

  if (!bulk) openAiModal(fname);
  else $("aiModalSub").textContent = `Processing: ${fname}`;
  setAiStep(0, "active", "Sending to server…");

  try {
    const preview = await api("POST", "/api/upload/receipt/preview", fd);
    setAiStep(
      0,
      "done",
      preview.auto_segmented
        ? `File received · auto-detected ${preview.bboxes.length} regions`
        : "File received",
    );

    if (preview.auto_segmented && !bulk) {
      // Single-file path: open the bbox editor for the user to confirm.
      // The editor owns the rest of this file's flow.
      closeAiModal();
      openBBoxEditor({
        filename: preview.filename,
        image_url: preview.image_url,
        image_size: preview.image_size,
        initialBoxes: preview.bboxes,
        description: desc,
      });
      return { ok: true, deferred: true };
    }

    // Auto-commit: either a single region was detected, OR we're in bulk
    // and we trust the segmenter's bboxes for every file.
    const r = await commitReceiptOCR({
      filename: preview.filename,
      bboxes: preview.bboxes,
      description: desc,
      fname,
      bulk,
    });
    return {
      ok: true,
      deferred: false,
      count: r.count,
      ids: r.ids,
      firstErr: r.firstErr,
      metas: r.metas,
    };
  } catch (e) {
    setAiStep(1, "error", e.message);
    return { ok: false, error: e.message };
  }
}

$("uploadReceiptBtn").addEventListener("click", async () => {
  if (receiptFiles.length === 0) return;
  const files = receiptFiles.slice(); // snapshot — user may pick more mid-run
  const desc = $("receiptDesc").value.trim();

  const uploadSvg = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`;

  $("uploadReceiptBtn").disabled = true;
  $("uploadReceiptBtn").innerHTML = '<span class="spin"></span> Uploading…';
  $("receiptStatus").textContent = "";

  const resetUploaderUI = () => {
    $("receiptFile").value = "";
    $("receiptFilename").textContent = "";
    receiptFiles = [];
    $("receiptDesc").value = "";
    $("uploadReceiptBtn").disabled = false;
    $("uploadReceiptBtn").innerHTML = `${uploadSvg} Upload &amp; Process`;
  };

  // ── Single-file path: preserve the existing UX (bbox editor allowed) ──
  if (files.length === 1) {
    const result = await _processSingleReceiptFile(files[0], desc, {
      bulk: false,
    });
    if (!result.ok) {
      $("aiModalSub").textContent = "Error: " + result.error;
      $("receiptStatus").textContent = "Error: " + result.error;
      $("receiptStatus").className = "status-msg err";
    }
    resetUploaderUI();
    return;
  }

  // ── Bulk path: run files sequentially, aggregate counts ──
  openAiModal(`Uploading ${files.length} files`);
  $("aiModalSub").textContent = `0 / ${files.length} processed`;

  let savedReceipts = 0; // total receipt rows created across all files
  let succeededFiles = 0; // files that returned without error
  let failedFiles = 0;
  const errors = [];

  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    $("uploadReceiptBtn").innerHTML =
      `<span class="spin"></span> ${i + 1}/${files.length}…`;
    $("aiModalTitle").textContent =
      `Uploading ${i + 1}/${files.length}: ${f.name}`;

    const result = await _processSingleReceiptFile(f, desc, { bulk: true });
    if (!result.ok) {
      failedFiles++;
      errors.push(`${f.name}: ${result.error}`);
    } else {
      succeededFiles++;
      savedReceipts += result.count || 0;
    }
    $("aiModalSub").textContent =
      `${i + 1} / ${files.length} processed · ${savedReceipts} receipts saved` +
      (failedFiles ? ` · ${failedFiles} failed` : "");
  }

  // Final summary
  const summary =
    failedFiles === 0
      ? `Saved ${savedReceipts} receipts from ${succeededFiles} files`
      : `${succeededFiles}/${files.length} files OK, ${failedFiles} failed — ${savedReceipts} receipts saved`;

  $("aiModalTitle").textContent =
    failedFiles === 0
      ? "Bulk upload complete"
      : "Bulk upload finished with errors";
  $("aiModalSub").textContent =
    summary + (errors.length ? `\n${errors.slice(0, 3).join(" · ")}` : "");
  $("receiptStatus").textContent = summary;
  $("receiptStatus").className =
    "status-msg " + (failedFiles === 0 ? "ok" : "err");

  loadReceipts();
  loadStats();
  resetUploaderUI();
  setTimeout(closeAiModal, failedFiles === 0 ? 4000 : 0);
});

// Click outside modal to close
$("aiModal").addEventListener("click", (e) => {
  if (e.target === $("aiModal")) closeAiModal();
});

// ─── Upload statement ─────────────────────────────────────────────────────────
let statementFileObj = null;

$("statementFile").addEventListener("change", () => {
  statementFileObj = $("statementFile").files[0] || null;
  $("uploadStatementBtn").disabled = !statementFileObj;
  $("statementFilename").textContent = statementFileObj
    ? statementFileObj.name
    : "";
  if (statementFileObj) {
    $("statementStatus").textContent = "";
    $("statementStatus").className = "status-msg";
  }
});

$("uploadStatementBtn").addEventListener("click", async () => {
  if (!statementFileObj) return;
  const fd = new FormData();
  fd.append("file", statementFileObj);

  $("statementStatus").textContent = "Parsing CSV…";
  $("statementStatus").className = "status-msg load";
  $("uploadStatementBtn").disabled = true;
  $("uploadStatementBtn").innerHTML = '<span class="spin"></span> Uploading…';

  try {
    const res = await api("POST", "/api/upload/statement", fd);
    $("statementStatus").textContent =
      `Saved — ${res.transaction_count} transactions`;
    $("statementStatus").className = "status-msg ok";
    $("statementFile").value = "";
    $("statementFilename").textContent = "";
    statementFileObj = null;
    loadStatements();
    loadStats();
  } catch (e) {
    $("statementStatus").textContent = "Error: " + e.message;
    $("statementStatus").className = "status-msg err";
  } finally {
    $("uploadStatementBtn").disabled = false;
    $("uploadStatementBtn").innerHTML =
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Upload Statement`;
  }
});

// ─── Upload Uber rides (bulk CSV) ─────────────────────────────────────────────
let uberFileObj = null;

$("uberFile").addEventListener("change", () => {
  uberFileObj = $("uberFile").files[0] || null;
  $("uploadUberBtn").disabled = !uberFileObj;
  $("uberFilename").textContent = uberFileObj ? uberFileObj.name : "";
  if (uberFileObj) {
    $("uberStatus").textContent = "";
    $("uberStatus").className = "status-msg";
  }
});

$("uploadUberBtn").addEventListener("click", async () => {
  if (!uberFileObj) return;
  const fd = new FormData();
  fd.append("file", uberFileObj);
  const businessOnly = $("uberBusinessOnly").checked;
  const after = $("uberAfter").value || "";
  const before = $("uberBefore").value || "";
  const qs = new URLSearchParams({ business_only: businessOnly });
  if (after) qs.set("after", after);
  if (before) qs.set("before", before);
  const url = `/api/upload/uber-csv?${qs.toString()}`;

  $("uploadUberBtn").disabled = true;
  $("uploadUberBtn").innerHTML = '<span class="spin"></span> Importing…';
  $("uberStatus").textContent = "Parsing CSV…";
  $("uberStatus").className = "status-msg load";

  try {
    const res = await api("POST", url, fd);
    const parts = [`Imported ${res.imported}`];
    if (res.skipped_duplicates)
      parts.push(`${res.skipped_duplicates} duplicates skipped`);
    if (res.parsed_rows !== res.imported + res.skipped_duplicates) {
      parts.push(
        `${res.parsed_rows - res.imported - res.skipped_duplicates} filtered`,
      );
    }
    $("uberStatus").textContent = parts.join(" · ");
    $("uberStatus").className = "status-msg ok";
    $("uberFile").value = "";
    $("uberFilename").textContent = "";
    uberFileObj = null;
    loadReceipts();
    loadStats();
  } catch (e) {
    $("uberStatus").textContent = "Error: " + e.message;
    $("uberStatus").className = "status-msg err";
  } finally {
    $("uploadUberBtn").disabled = false;
    $("uploadUberBtn").innerHTML =
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Import Uber rides`;
  }
});

// ─── Uber via Gmail (Phase B) ─────────────────────────────────────────────────
async function loadGmailStatus() {
  let st;
  try {
    st = await api("GET", "/api/gmail/status");
  } catch {
    return;
  }
  const card = $("gmailCard");
  // Hide the card entirely if OAuth client isn't even present — keeps the
  // UI uncluttered for users who don't want this feature.
  if (!st.credentials_file_present) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";

  const badge = $("gmailStatusBadge");
  const hint = $("gmailHint");
  const connectBtn = $("gmailConnectBtn");
  const syncGroup = $("gmailConnectedGroup");

  if (st.configured) {
    badge.textContent = "Connected";
    badge.className = "badge badge-green";
    hint.textContent = "Attaches PDFs from your inbox to existing Uber rides.";
    connectBtn.style.display = "none";
    syncGroup.style.display = "";
  } else {
    badge.textContent = "Not connected";
    badge.className = "badge badge-amber";
    hint.textContent = "Sign in to attach PDF receipts to your Uber rides.";
    connectBtn.style.display = "";
    syncGroup.style.display = "none";
  }
}

// Shared OAuth-start handler — used by both the initial Connect button
// (token missing) and the Reconnect button (token expired / revoked).
async function startGmailOAuth(btn) {
  const status = $("gmailStatus");
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Waiting for browser…';
  status.textContent =
    "Granting consent in browser. Your default browser should have opened.";
  status.className = "status-msg load";
  try {
    await api("POST", "/api/gmail/auth/start");
    status.textContent = "Connected. You can now sync rides.";
    status.className = "status-msg ok";
    await loadGmailStatus();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

$("gmailReconnectBtn").addEventListener("click", () =>
  startGmailOAuth($("gmailReconnectBtn")),
);

// Replace credentials.json via a file picker. Used when the original OAuth
// client's Google Cloud project was deleted or the client was rotated.
async function uploadGmailCredentials(file) {
  const status = $("gmailStatus");
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  status.textContent = "Uploading new credentials.json…";
  status.className = "status-msg load";
  try {
    const res = await fetch("/api/gmail/credentials", {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    status.textContent =
      "credentials.json updated. Now reconnect to grant access.";
    status.className = "status-msg ok";
    await loadGmailStatus();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  }
}

$("gmailCredsFile").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) uploadGmailCredentials(f);
  e.target.value = ""; // allow re-uploading the same file
});

$("gmailConnectBtn").addEventListener("click", () =>
  startGmailOAuth($("gmailConnectBtn")),
);

function renderLastSync(res) {
  // Compact one-line summary persisted in localStorage so it stays visible
  // across reloads until the next sync overwrites it.
  const parts = [];
  if (res.upgraded) parts.push(`${res.upgraded} attached`);
  if (res.imported) parts.push(`${res.imported} new`);
  if (res.unmatched && res.unmatched.length)
    parts.push(`${res.unmatched.length} unmatched`);
  if (!parts.length && res.scanned) parts.push("no changes");
  const when = new Date().toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
  const text = parts.length
    ? `Last sync: ${when} · ${parts.join(" · ")}`
    : `Last sync: ${when}`;
  try {
    localStorage.setItem("gmailLastSync", text);
  } catch {}
  const el = $("gmailLastSync");
  if (el) el.textContent = text;
}

function restoreLastSync() {
  try {
    const t = localStorage.getItem("gmailLastSync");
    if (t) {
      const el = $("gmailLastSync");
      if (el) el.textContent = t;
    }
  } catch {}
}

$("gmailSyncBtn").addEventListener("click", async () => {
  const btn = $("gmailSyncBtn");
  const status = $("gmailStatus");
  const businessOnly = $("gmailBusinessOnly").checked;
  // Inverted from prior "attach-only" checkbox: ticked = also create new
  // rows for unmatched emails. Default unticked = attach-only.
  const attachOnly = !$("gmailAddNewRows").checked;
  const after = $("gmailAfter").value || "";
  const before = $("gmailBefore").value || "";

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Syncing…';
  status.textContent = "";

  const qs = new URLSearchParams({
    business_only: businessOnly,
    attach_only: attachOnly,
  });
  if (after) qs.set("after", after);
  if (before) qs.set("before", before);

  try {
    const res = await api("POST", `/api/gmail/sync?${qs.toString()}`);
    renderLastSync(res);
    status.textContent = "";
    status.className = "status-msg";
    if (res.unmatched && res.unmatched.length) {
      // Surface a few examples so the user can spot-check what was skipped.
      const preview = res.unmatched
        .slice(0, 5)
        .map(
          (u) =>
            `${u.date || "????"} · ${u.currency || ""}${u.amount ?? "?"} · ${(u.subject || "").slice(0, 50)}`,
        )
        .join("<br>");
      const more =
        res.unmatched.length > 5
          ? `<br>… and ${res.unmatched.length - 5} more`
          : "";
      status.innerHTML = `<div style="font-size:11px;color:var(--text-3);line-height:1.5">${preview}${more}</div>`;
    }
    loadReceipts();
    loadStats();
  } catch (e) {
    // The server returns structured errors {error, error_kind} for sync;
    // try to parse them out of the FastAPI HTTPException detail.
    let errorKind = null;
    let errorMsg = e.message || String(e);
    try {
      const parsed = JSON.parse(e.message);
      if (parsed && typeof parsed === "object") {
        errorKind = parsed.error_kind || null;
        errorMsg = parsed.error || errorMsg;
      }
    } catch {}
    // Fallback heuristic when the server didn't categorise it (raw exception
    // bubbled through, or a network-layer failure).
    if (!errorKind) {
      if (/project.*deleted/i.test(errorMsg)) errorKind = "project_deleted";
      else if (/invalid_client/i.test(errorMsg)) errorKind = "invalid_client";
      else if (
        /invalid_grant|token (has been )?expired|RefreshError/i.test(errorMsg)
      )
        errorKind = "token_expired";
      else if (/not authenticated/i.test(errorMsg)) errorKind = "token_missing";
    }

    const reconnectBtn = `<button class="btn btn-outline btn-sm" id="gmailInlineReconnect" style="margin-top:6px"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Reconnect Gmail</button>`;
    const replaceCredsBtn = `<label class="btn btn-outline btn-sm" id="gmailInlineReplace" style="margin-top:6px;margin-left:6px;cursor:pointer"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Replace credentials.json<input type="file" id="gmailInlineCredsFile" accept="application/json,.json" style="display:none"></label>`;

    if (errorKind === "project_deleted" || errorKind === "invalid_client") {
      status.innerHTML =
        `<div>${errorMsg}</div>` +
        `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">${replaceCredsBtn}</div>`;
      status.className = "status-msg err";
      const f = $("gmailInlineCredsFile");
      if (f)
        f.addEventListener("change", (e) => {
          const file = e.target.files && e.target.files[0];
          if (file) uploadGmailCredentials(file);
        });
    } else if (errorKind === "token_expired" || errorKind === "token_missing") {
      status.innerHTML = `<div>${errorMsg}</div>${reconnectBtn}`;
      status.className = "status-msg err";
      const ib = $("gmailInlineReconnect");
      if (ib) ib.addEventListener("click", () => startGmailOAuth(ib));
    } else if (errorKind === "api_disabled") {
      status.innerHTML =
        `<div>${errorMsg}</div>` +
        `<div style="font-size:11px;color:var(--text-3);margin-top:4px">` +
        `Open <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">Google Cloud Console → Gmail API</a> and click Enable.</div>`;
      status.className = "status-msg err";
    } else {
      status.textContent = "Error: " + errorMsg;
      status.className = "status-msg err";
    }
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Sync receipts`;
  }
});

$("dedupeUberBtn").addEventListener("click", async () => {
  const btn = $("dedupeUberBtn");
  const status = $("uberStatus");
  if (
    !confirm(
      "Merge Uber rides that share date + amount + currency? The PDF-bearing row wins, others are deleted.",
    )
  )
    return;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Deduping…';
  status.textContent = "";
  try {
    const res = await api("POST", "/api/receipts/dedupe-uber");
    status.textContent = `Removed ${res.removed} duplicates across ${res.groups_with_dupes} groups`;
    status.className = "status-msg ok";
    loadReceipts();
    loadStats();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><rect x="9" y="10" width="6" height="6" rx="1"/></svg> Dedupe Uber rides`;
  }
});

// ─── Drag & drop ──────────────────────────────────────────────────────────────
function setupDrop(zoneId, inputId) {
  const zone = $(zoneId),
    input = $(inputId);
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    if (!e.dataTransfer.files.length) return;
    const dt = new DataTransfer();
    // Inputs without `multiple` only accept the first file — preserve old
    // single-file semantics for statement / uber CSV drops by gating on the
    // attribute. Receipt input has `multiple` so all dropped files pass.
    const accept = input.multiple
      ? e.dataTransfer.files
      : [e.dataTransfer.files[0]];
    for (const f of accept) dt.items.add(f);
    input.files = dt.files;
    input.dispatchEvent(new Event("change"));
  });
}
setupDrop("receiptDrop", "receiptFile");
setupDrop("statementDrop", "statementFile");
setupDrop("uberDrop", "uberFile");

// ─── Thumbnail fallback ───────────────────────────────────────────────────────
// Called from <img onerror="handleThumbError(this)"> when a receipt thumbnail
// can't be loaded (corrupt file, server cold, etc.). Replaces the broken
// <img> with a placeholder document icon.
function handleThumbError(img) {
  const src = img.dataset.fallbackSrc || img.src;
  const id = img.dataset.receiptId ? parseInt(img.dataset.receiptId, 10) : null;
  const div = document.createElement("div");
  div.className = "thumb-ph";
  div.addEventListener("click", () => openLightbox(src, id));
  div.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
    <polyline points="14 2 14 8 20 8"/></svg>`;
  img.replaceWith(div);
}

// ─── Lightbox ─────────────────────────────────────────────────────────────────
// Tracks the receipt id currently being viewed so the toolbar (rotate /
// download) can address the right row. PDFs and bytes-only previews can
// still open in the lightbox, but the rotate button is disabled for them.
let _lbReceiptId = null;
let _lbDownloadUrl = null;

function openLightbox(src, receiptId) {
  const lb = $("lightbox"),
    img = $("lbImg"),
    spin = $("lbSpin");
  img.style.display = "none";
  spin.style.display = "flex";
  lb.classList.add("open");
  document.body.style.overflow = "hidden";
  img.onload = () => {
    spin.style.display = "none";
    img.style.display = "";
  };
  img.onerror = () => {
    spin.innerHTML =
      '<span style="font-size:2rem;opacity:.5">⚠</span><span style="font-size:13px;color:#fff9">Could not load</span>';
  };
  // Cache-buster on every open so the displayed image reflects the latest
  // on-disk state (rotation rewrites the same path).
  img.src = src + (src.includes("?") ? "&" : "?") + "t=" + Date.now();
  _lbReceiptId = receiptId ?? null;
  _lbDownloadUrl = src;
  $("lbRotateLeft").disabled = !receiptId;
  $("lbRotateRight").disabled = !receiptId;
}
function closeLightbox(e) {
  if (e && e.target !== $("lightbox") && e.target !== $("lbClose")) return;
  $("lightbox").classList.remove("open");
  $("lbImg").src = "";
  document.body.style.overflow = "";
  _lbReceiptId = null;
  _lbDownloadUrl = null;
}

async function _lbRotate(degrees) {
  if (_lbReceiptId == null) return;
  const left = $("lbRotateLeft");
  const right = $("lbRotateRight");
  left.disabled = right.disabled = true;
  try {
    await api(
      "POST",
      `/api/receipts/${_lbReceiptId}/rotate?degrees=${degrees}`,
    );
    // Reload the lightbox image + the table thumbnail
    if (_lbDownloadUrl) openLightbox(_lbDownloadUrl, _lbReceiptId);
    loadReceipts();
  } catch (e) {
    alert("Rotate failed: " + e.message);
  } finally {
    left.disabled = right.disabled = false;
  }
}

$("lbRotateLeft").addEventListener("click", () => _lbRotate(-90));
$("lbRotateRight").addEventListener("click", () => _lbRotate(90));
$("lbDownload").addEventListener("click", () => {
  if (!_lbDownloadUrl) return;
  const a = document.createElement("a");
  a.href = _lbDownloadUrl;
  // Strip cache-buster querystring for a cleaner download filename
  const url = _lbDownloadUrl.split("?")[0];
  a.download = url.split("/").pop() || "receipt";
  document.body.appendChild(a);
  a.click();
  a.remove();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeLightbox();
    closeAiModal();
    closeEditModal();
  }
});

// ─── Init ─────────────────────────────────────────────────────────────────────
loadStats().catch(() => {});
loadReceipts().catch(() => {});
loadFxRates().catch(() => {});
loadWatchFolder().catch(() => {});
loadGmailStatus().catch(() => {});
restoreLastSync();
checkOllama().catch(() => {});
