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
  // 204 No Content carries no body — DELETE /api/matches/{id} returns that
  // and the previous unconditional res.json() threw a SyntaxError that bled
  // through as "Failed to unlink" flash messages on the reconcile board.
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
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
    // ISO YYYY-MM-DD — build the Date from local-time components, NOT via
    // `new Date(string)`. The string form gets parsed as UTC midnight and
    // shifts back a day for users in negative UTC offsets (e.g. BRT, PST).
    const iso = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (iso) {
      const d = new Date(+iso[1], +iso[2] - 1, +iso[3]);
      return d.toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      });
    }
    // Other formats (e.g. dd/mm/yyyy): rewrite to ISO and recurse via Date.
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

// ─── Receipts filter + sort state ────────────────────────────────────────────
// Stored in localStorage so it survives page reloads. The filter UI lives in
// `#receiptFilterBar` (HTML) — wiring is in _wireReceiptFilters() at file end.
const _FILTER_DEFAULTS = {
  q: "",
  from: "",
  to: "",
  min: "",
  max: "",
  // "active" = the default bucket: hides matched (archived) receipts.
  status: "active",
  category: "",
  currency: "",
};
let receiptFilters = (() => {
  let f;
  try {
    f = {
      ..._FILTER_DEFAULTS,
      ...JSON.parse(localStorage.getItem("rt.filter.receipts") || "{}"),
    };
  } catch {
    f = { ..._FILTER_DEFAULTS };
  }
  // Migrate the legacy empty "all" status to the new "active" default, so
  // matched (archived) receipts are hidden from the main bucket by default.
  if (!f.status) f.status = "active";
  return f;
})();
let receiptSort = (() => {
  try {
    return (
      JSON.parse(localStorage.getItem("rt.sort.receipts") || "null") || {
        col: "date",
        dir: "desc",
      }
    );
  } catch {
    return { col: "date", dir: "desc" };
  }
})();
let matchSort = (() => {
  try {
    return (
      JSON.parse(localStorage.getItem("rt.sort.matches") || "null") || {
        col: "score",
        dir: "desc",
      }
    );
  } catch {
    return { col: "score", dir: "desc" };
  }
})();

function _saveReceiptFilters() {
  localStorage.setItem("rt.filter.receipts", JSON.stringify(receiptFilters));
}
function _saveSort(key, state) {
  localStorage.setItem(`rt.sort.${key}`, JSON.stringify(state));
}

function applyReceiptFilters(receipts, opts = {}) {
  const f = receiptFilters;
  return receipts.filter((r) => {
    const m = (() => {
      try {
        return JSON.parse(r.metadata || "{}");
      } catch {
        return {};
      }
    })();
    if (f.q) {
      const q = f.q.toLowerCase();
      const hay = [
        m.vendor_name || "",
        r.description || "",
        m.city || "",
        m.state || "",
        m.country || "",
        m.vendor_address || "",
      ]
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    const date = (m.date || "").slice(0, 10);
    if (f.from && date && date < f.from) return false;
    if (f.to && date && date > f.to) return false;
    if (f.from && !date) return false; // can't include rows without a date when a date filter is on
    if (f.to && !date) return false;
    const amt = parseFloat(m.total_amount);
    if (f.min !== "") {
      const min = parseFloat(f.min);
      if (isNaN(amt) || amt < min) return false;
    }
    if (f.max !== "") {
      const max = parseFloat(f.max);
      if (isNaN(amt) || amt > max) return false;
    }
    if (!opts.ignoreStatus) {
      const st = f.status || "active";
      const ms = r.match_status || "pending";
      // "active" hides archived (matched) receipts; "all" shows everything;
      // anything else is an exact status match.
      if (st === "active") {
        if (ms === "matched") return false;
      } else if (st !== "all" && ms !== st) {
        return false;
      }
    }
    if (f.category && (m.category || "other") !== f.category) return false;
    if (f.currency && (m.currency || "") !== f.currency) return false;
    return true;
  });
}

function sortReceiptList(receipts, sort) {
  const { col, dir } = sort;
  const mult = dir === "desc" ? -1 : 1;
  const get = (r) => {
    const m = (() => {
      try {
        return JSON.parse(r.metadata || "{}");
      } catch {
        return {};
      }
    })();
    switch (col) {
      case "date":
        return m.date || "";
      case "vendor":
        return (m.vendor_name || "").toLowerCase();
      case "amount":
        return parseFloat(m.total_amount) || 0;
      case "category":
        return m.category || "";
      case "status":
        return r.match_status || "pending";
      default:
        return "";
    }
  };
  return [...receipts].sort((a, b) => {
    const va = get(a),
      vb = get(b);
    if (va < vb) return -1 * mult;
    if (va > vb) return 1 * mult;
    return 0;
  });
}

function _refreshSortIndicators(tableKey, sort) {
  document
    .querySelectorAll(`table.sortable[data-table="${tableKey}"] th[data-sort]`)
    .forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.dataset.sort === sort.col) th.classList.add(`sort-${sort.dir}`);
    });
}

function _updateFilterBadge() {
  const f = receiptFilters;
  // Count populated filter fields (anything non-empty counts as 1 active).
  // The default "active" status is not a user-applied filter, so don't count it.
  const count = Object.entries(f).filter(
    ([k, v]) => v !== "" && v != null && !(k === "status" && v === "active"),
  ).length;
  const badge = $("rfActiveCount");
  const clear = $("rfClearBtn");
  if (!badge || !clear) return;
  if (count > 0) {
    badge.textContent = `${count} active`;
    badge.style.display = "inline-flex";
    clear.style.display = "inline-flex";
  } else {
    badge.style.display = "none";
    clear.style.display = "none";
  }
}

function populateCurrencyFilter(receipts) {
  // Build the currency dropdown from whatever currencies the user actually
  // has in their data — no point listing AUD if no row uses it.
  const sel = $("rfCurrency");
  if (!sel) return;
  const seen = new Set();
  receipts.forEach((r) => {
    try {
      const c = JSON.parse(r.metadata || "{}").currency;
      if (c) seen.add(c);
    } catch {}
  });
  const existing = new Set([...sel.options].map((o) => o.value));
  [...seen].sort().forEach((c) => {
    if (!existing.has(c)) {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      sel.appendChild(opt);
    }
  });
  // Restore the persisted selection now that the option exists.
  if (
    receiptFilters.currency &&
    [...sel.options].some((o) => o.value === receiptFilters.currency)
  ) {
    sel.value = receiptFilters.currency;
  }
}

// ─── Progress strip ───────────────────────────────────────────────────────────
function updateProgress(receipts) {
  // Progress chip reflects whatever's currently visible — month + filters.
  // Keeping these in sync avoids confusion when the user narrows to e.g.
  // "category=meals" and the chip would otherwise show all-category counts.
  // Ignore the status filter here so archiving (hiding matched) doesn't zero
  // out the reconciliation progress — the strip always reflects real counts.
  const filtered = applyReceiptFilters(filterByMonth(receipts), {
    ignoreStatus: true,
  });
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
  // Pipeline: month filter → filter-bar filters → sort. All three are
  // pure client-side over `allReceipts` so the response shape from the API
  // hasn't changed and the existing edit / delete / bulk paths still work.
  let filtered = filterByMonth(receipts);
  filtered = applyReceiptFilters(filtered);
  filtered = sortReceiptList(filtered, receiptSort);
  const tbody = $("receiptsTbody");
  $("navReceiptCount").textContent = filtered.length;

  if (!filtered.length) {
    // If the active bucket is empty only because matched receipts are
    // archived, point the user at the Archived/All filter instead of "no
    // receipts".
    const hiddenArchived =
      receiptFilters.status === "active" &&
      filterByMonth(receipts).some((r) => r.match_status === "matched");
    const msg = hiddenArchived
      ? "All receipts here are archived"
      : currentMonth === "all"
        ? "No receipts yet"
        : "No receipts for this month";
    const sub = hiddenArchived
      ? "They're matched. Set the status filter to <b>Archived</b> or <b>All</b> to view them."
      : "Upload a receipt image or PDF to get started.";
    tbody.innerHTML = `<tr class="empty"><td colspan="13">
      <div class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        <h3>${msg}</h3>
        <p>${sub}</p>
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
    populateCurrencyFilter(receipts);
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
  let matches = [];
  try {
    // Fetch statements + matches in parallel so each row can show how many
    // of its transactions have already been paired.
    const [sRes, mRes] = await Promise.all([
      api("GET", "/api/statements"),
      api("GET", "/api/matches").catch(() => ({ matches: [] })),
    ]);
    statements = sRes.statements || [];
    matches = mRes.matches || [];
  } catch {}
  $("navStmtCount").textContent = statements.length;

  // Count matches per statement_id in one pass.
  const matchedCount = {};
  matches.forEach((m) => {
    matchedCount[m.statement_id] = (matchedCount[m.statement_id] || 0) + 1;
  });

  const tbody = $("statementsTbody");
  if (!statements.length) {
    tbody.innerHTML = `<tr class="empty"><td colspan="8">
      <div class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <h3>No statements yet</h3><p>Upload a corporate card CSV or PDF to begin reconciliation.</p>
      </div>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = statements
    .map((s) => {
      // `transactions` arrives as a JSON string on the wire.
      let txns = s.transactions || [];
      if (typeof txns === "string") {
        try {
          txns = JSON.parse(txns);
        } catch {
          txns = [];
        }
      }
      // The statement bill is always in BRL (Itaú), and international rows
      // already roll up into a BRL amount. So a plain sum of `amount`
      // gives the user the bill total they'd recognise.
      let total = 0;
      const dates = [];
      txns.forEach((t) => {
        const amt = parseFloat(t.amount);
        if (!isNaN(amt)) total += amt;
        if (t.date) dates.push(t.date);
      });
      const period =
        dates.length === 0
          ? "—"
          : `${fmt.date(dates.reduce((a, b) => (a < b ? a : b)))} → ${fmt.date(
              dates.reduce((a, b) => (a > b ? a : b)),
            )}`;
      const totalLabel = txns.length ? fmt.currency(total, "BRL") : "—";
      const matched = matchedCount[s.id] || 0;
      const matchedChip =
        matched === 0
          ? `<span class="badge badge-gray">0 / ${txns.length}</span>`
          : matched >= txns.length
            ? `<span class="badge badge-green">${matched} / ${txns.length}</span>`
            : `<span class="badge badge-amber">${matched} / ${txns.length}</span>`;

      return `<tr class="data-row clickable" onclick="openStatementDetail(${s.id})">
    <td style="color:var(--text-3);font-family:'DM Mono',monospace;font-size:12px">#${s.id}</td>
    <td class="fname" title="${s.filename}" style="font-weight:500">${fmt.file(s.filename)}</td>
    <td><span class="badge badge-gray">${txns.length} txns</span></td>
    <td style="text-align:right;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap">${totalLabel}</td>
    <td style="color:var(--text-2);font-size:12px;white-space:nowrap">${period}</td>
    <td>${matchedChip}</td>
    <td style="color:var(--text-2);font-size:12px">${(s.uploaded_at || "").slice(0, 16).replace("T", " ")}</td>
    <td><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); deleteStatement(${s.id})">Del</button></td>
  </tr>`;
    })
    .join("");
}

async function deleteStatement(id) {
  if (!confirm("Remove this statement and its match results?")) return;
  await api("DELETE", `/api/statements/${id}`);
  loadStatements();
  loadReceipts();
  loadStats();
  // The Reconcile board pulls its own data when the tab opens, so no
  // imperative refresh needed here — it picks up the change next visit.
}

// ─── Statement detail modal (clicking a row on the Statements tab) ──────────
let _stmtDetailRows = []; // working copy filtered by the search box
let _stmtDetailAll = []; // full transactions array — search filters against this

async function openStatementDetail(id) {
  $("stmtDetailModal").classList.add("open");
  $("stmtDetailTitle").textContent = "Statement";
  $("stmtDetailSub").textContent = "Loading…";
  $("stmtDetailTbody").innerHTML = "";
  $("stmtDetailSum").textContent = "—";
  $("stmtDetailQ").value = "";

  try {
    const res = await api("GET", `/api/statements/${id}/transactions`);
    const s = res.statement || {};
    _stmtDetailAll = res.transactions || [];
    _stmtDetailRows = _stmtDetailAll;

    $("stmtDetailTitle").textContent = s.filename || `Statement #${id}`;
    $("stmtDetailSub").textContent = `${_stmtDetailAll.length} transactions`;
    _renderStmtDetail();
  } catch (e) {
    $("stmtDetailSub").innerHTML =
      `<span class="status-msg err">Failed to load: ${e.message}</span>`;
  }
}

function _renderStmtDetail() {
  const tbody = $("stmtDetailTbody");
  if (!_stmtDetailRows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="stmt-detail-empty">No transactions match your search.</td></tr>`;
    $("stmtDetailSum").textContent = "—";
    return;
  }
  tbody.innerHTML = _stmtDetailRows
    .map((t) => {
      const isIntl =
        t.original_currency &&
        t.original_amount != null &&
        t.original_currency !== "BRL";
      // A txn can have multiple matches (cumulative pairing). Use the
      // backend-provided `matches` array; fall back to the legacy `match`
      // field for any cached older responses.
      const matchArr = Array.isArray(t.matches)
        ? t.matches
        : t.match
          ? [t.match]
          : [];
      const matched = matchArr.length > 0;
      let matchLabel;
      if (!matched) {
        matchLabel = `<span class="col-match unmatched">—</span>`;
      } else if (matchArr.length === 1) {
        const m = matchArr[0];
        matchLabel = `<span class="col-match matched" title="${_esc(m.receipt_vendor || "receipt")} · ${fmt.currency(m.receipt_amount, m.receipt_currency)}">✓ ${_esc(m.receipt_vendor || "matched")}</span>`;
      } else {
        // ≥2 cumulative receipts — show count + tooltip with each line.
        const tip = matchArr
          .map(
            (m) =>
              `${m.receipt_vendor || "receipt"} · ${fmt.currency(m.receipt_amount, m.receipt_currency)}`,
          )
          .join("\n");
        matchLabel = `<span class="col-match matched" title="${_esc(tip)}">✓ ${matchArr.length} receipts</span>`;
      }
      // Headline shows native for international rows (receipts always come
      // in native currency), with the BRL bill total under it. Tooltip
      // exposes the FX rate so the user can sanity-check the conversion.
      const amountCell = isIntl
        ? `<div class="col-amount intl" title="FX: R$ ${(t.fx_to_brl || 0).toFixed(2)} / USD">
             <div>${fmt.currency(t.original_amount, t.original_currency)}</div>
             <div class="col-amount-sub">${fmt.currency(t.amount, "BRL")}</div>
           </div>`
        : `<div class="col-amount">${fmt.currency(t.amount, t.currency || "BRL")}</div>`;
      return `<tr>
        <td class="col-date">${fmt.date(t.date)}</td>
        <td class="col-desc" title="${_esc(t.description)}">${_esc(t.description)}</td>
        <td>${amountCell}</td>
        <td>${t.category ? `<span class="badge badge-gray">${_esc(t.category)}</span>` : '<span style="color:var(--text-3)">—</span>'}</td>
        <td class="col-city">${t.city ? _esc(t.city) : "—"}</td>
        <td>${matchLabel}</td>
      </tr>`;
    })
    .join("");

  // Sum line: BRL bill total (that's what the user owes) + foreign-currency
  // totals derived from `original_*` so the user sees "R$ 22,250 · CAD
  // 1,260 · USD 145" for a Brazilian statement with international rows.
  let brlTotal = 0;
  const foreignTotals = {};
  _stmtDetailRows.forEach((t) => {
    brlTotal += parseFloat(t.amount) || 0;
    if (
      t.original_currency &&
      t.original_currency !== "BRL" &&
      t.original_amount != null
    ) {
      foreignTotals[t.original_currency] =
        (foreignTotals[t.original_currency] || 0) +
        (parseFloat(t.original_amount) || 0);
    }
  });
  const parts = [`<strong>${fmt.currency(brlTotal, "BRL")}</strong>`];
  Object.entries(foreignTotals).forEach(([cur, sum]) => {
    parts.push(`<strong>${fmt.currency(sum, cur)}</strong>`);
  });
  $("stmtDetailSum").innerHTML =
    `${_stmtDetailRows.length} shown · ${parts.join(" · ")}`;
}

function closeStatementDetail() {
  $("stmtDetailModal").classList.remove("open");
  _stmtDetailAll = [];
  _stmtDetailRows = [];
}

// Small local escape helper — `_esc` exists inside the Reconcile module
// closure but isn't reachable here. Define a sibling for this module.
function _esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

// Wire the search box + dismiss handlers once.
(function _wireStmtDetailModal() {
  const q = $("stmtDetailQ");
  if (!q) return;
  q.addEventListener("input", () => {
    const term = q.value.trim().toLowerCase();
    if (!term) {
      _stmtDetailRows = _stmtDetailAll;
    } else {
      _stmtDetailRows = _stmtDetailAll.filter(
        (t) =>
          (t.description || "").toLowerCase().includes(term) ||
          (t.city || "").toLowerCase().includes(term) ||
          (t.category || "").toLowerCase().includes(term),
      );
    }
    _renderStmtDetail();
  });
  $("stmtDetailModal").addEventListener("click", (e) => {
    if (e.target === $("stmtDetailModal")) closeStatementDetail();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("stmtDetailModal").classList.contains("open")) {
      closeStatementDetail();
    }
  });
})();

// ─── Reconcile (legacy renderer — superseded by the Reconcile module below) ──
// Kept around briefly to avoid runtime errors if any old code path still
// references `renderReconcile`. New flow is the drag-drop board.
function _renderReconcileLegacyUnused(result) {
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

  // Legacy renderer end — not wired anywhere now.
  return html;
}

// ─── Reconcile board ────────────────────────────────────────────────────────
// Drag-and-drop reconciliation: pair a receipt (right pane) with a statement
// transaction (left pane) by dragging. Auto-match runs the legacy engine and
// pre-populates the board. Manual matches POST /api/matches; unlink hits
// DELETE /api/matches/{id}.
const Reconcile = (() => {
  const reconState = {
    statementId: null,
    statements: [], // [{id, filename, transaction_count}]
    txns: [], // working copy from /statements/{id}/transactions
    receipts: [], // all receipts (with `matched` flag derived locally)
    showMatched: false,
    // Default: hide matched transactions so the left pane mirrors the
    // right pane (unmatched-only). Tick the checkbox to bring matched
    // rows back into view alongside the unmatched ones.
    showMatchedTxns: false,
    txnFilter: _loadFilter("rt.recon.txnFilter"),
    rcptFilter: _loadFilter("rt.recon.receiptFilter"),
    dragging: null,
    initialized: false,
    autoMatchRunning: false,
    editingNote: null, // transaction index whose justification is being edited
  };

  function _loadFilter(key) {
    try {
      return JSON.parse(localStorage.getItem(key)) || {};
    } catch {
      return {};
    }
  }
  function _saveFilter(key, val) {
    try {
      localStorage.setItem(key, JSON.stringify(val));
    } catch {}
  }

  const _fmt = {
    money(v, cur) {
      if (v == null) return "—";
      const sym =
        cur === "BRL" ? "R$" : cur === "EUR" ? "€" : cur === "GBP" ? "£" : "$";
      return `${sym} ${parseFloat(v).toFixed(2)}`;
    },
    date(s) {
      if (!s) return "—";
      const iso = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
      const d = iso
        ? new Date(+iso[1], +iso[2] - 1, +iso[3])
        : new Date(s + "T12:00:00");
      if (isNaN(d)) return s;
      const months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
      ];
      return `${months[d.getMonth()]} ${d.getDate().toString().padStart(2, "0")}`;
    },
    cat(c) {
      if (!c) return "";
      return c.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
    },
  };

  function _dateDiffDays(a, b) {
    if (!a || !b) return 999;
    const da = new Date(a + "T12:00:00"),
      db = new Date(b + "T12:00:00");
    return Math.abs(Math.round((da - db) / 86400000));
  }

  function _esc(s) {
    return String(s == null ? "" : s).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  }

  function _isGoodMatch(rcpt, txn) {
    // Pick the right side of the txn to compare against the receipt:
    //   receipt is CAD, txn carries CAD as original_currency → compare CAD↔CAD
    //   otherwise fall back to BRL bill total ↔ receipt amount
    let txnAmt = txn.amount;
    const rcptCur = (rcpt.currency || "").toUpperCase();
    const nativeCur = (txn.original_currency || "").toUpperCase();
    if (
      rcptCur &&
      nativeCur &&
      rcptCur === nativeCur &&
      txn.original_amount != null
    ) {
      txnAmt = txn.original_amount;
    }
    return (
      Math.abs((rcpt.amount || 0) - (txnAmt || 0)) < 0.5 &&
      _dateDiffDays(rcpt.date, txn.date) <= 3
    );
  }

  // ── Bootstrap: only runs when the user lands on the Reconcile tab ────
  async function init() {
    if (reconState.initialized) {
      // Re-load receipts (and a fresh transactions list) every time the
      // tab is opened so newly added receipts show up.
      await _refreshAll();
      return;
    }
    reconState.initialized = true;
    _bindHeader();
    _bindFilters();
    await _refreshAll();
  }

  async function _refreshAll() {
    try {
      const [stmts, rcpts] = await Promise.all([
        api("GET", "/api/statements"),
        api("GET", "/api/receipts"),
      ]);
      reconState.statements = (stmts.statements || []).map((s) => {
        // The /statements API returns `transactions` already parsed into
        // a JS array (the backend deserialises before sending). Support
        // either shape just in case, and only fall back to 0 when the
        // payload is truly empty.
        let n = s.transaction_count;
        if (n == null) {
          if (Array.isArray(s.transactions)) {
            n = s.transactions.length;
          } else if (typeof s.transactions === "string") {
            try {
              n = JSON.parse(s.transactions || "[]").length;
            } catch {
              n = 0;
            }
          } else {
            n = 0;
          }
        }
        return { id: s.id, filename: s.filename, transaction_count: n };
      });
      // Normalise receipts for board use: pull amount/date/vendor out of metadata.
      reconState.receipts = (rcpts.receipts || []).map((r) => {
        const m = (() => {
          try {
            return JSON.parse(r.metadata || "{}");
          } catch {
            return {};
          }
        })();
        return {
          id: r.id,
          filename: r.filename || "",
          vendor: m.vendor_name || "Unknown",
          amount: m.total_amount != null ? parseFloat(m.total_amount) : null,
          currency: m.currency || null,
          date: m.date || (r.created_at || "").slice(0, 10),
          category: m.category || null,
          matched: r.match_status === "matched",
          expense_type: r.expense_type || "corp_card",
        };
      });
      // Ascending by date — pairs visually with the txns pane.
      reconState.receipts.sort((a, b) =>
        (a.date || "").localeCompare(b.date || ""),
      );
    } catch (e) {
      _flash("err", "Failed to load reconcile data: " + e.message);
      return;
    }
    _renderStatementSelect();
    // Default to the most recent statement if none picked yet.
    if (reconState.statementId == null && reconState.statements.length) {
      reconState.statementId = reconState.statements[0].id;
    }
    if (reconState.statementId != null) {
      await _selectStatement(reconState.statementId);
    } else {
      reconState.txns = [];
      _render();
    }
  }

  function _renderStatementSelect() {
    const sel = $("reconStatement");
    if (!reconState.statements.length) {
      sel.innerHTML = `<option value="">No statements uploaded yet</option>`;
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = reconState.statements
      .map(
        (s) =>
          `<option value="${s.id}">${_esc(s.filename)} · ${s.transaction_count} txns</option>`,
      )
      .join("");
    if (reconState.statementId != null) sel.value = reconState.statementId;
  }

  async function _selectStatement(id) {
    reconState.statementId = id;
    try {
      const res = await api("GET", `/api/statements/${id}/transactions`);
      // Sort ascending by date so receipts and txns line up chronologically
      // (makes drag-drop targeting much easier on long statements).
      reconState.txns = (res.transactions || [])
        .map((t) => ({ ...t }))
        .sort((a, b) => (a.date || "").localeCompare(b.date || ""));
    } catch (e) {
      _flash("err", `Failed to load transactions: ${e.message}`);
      reconState.txns = [];
    }
    _render();
  }

  function _render() {
    _renderHeaderCounts();
    _populateCurrencyDropdowns();
    _renderTxnList();
    _renderReceiptList();
  }

  function _renderHeaderCounts() {
    const total = reconState.txns.length;
    const matched = reconState.txns.filter((t) => _txnMatches(t).length).length;
    $("reconPairedNum").textContent = matched;
    $("reconPairedTotal").textContent = total;
    $("reconPairedLeft").textContent = Math.max(0, total - matched);
  }

  // Effective currency for filter purposes: an int'l txn carries the
  // native ccy in `original_currency`; everything else is BRL (the bill
  // amount the statement actually charges).
  function _txnCurrency(t) {
    return (t.original_currency || "BRL").toUpperCase();
  }

  function _matchesTxnFilter(t, f) {
    if (f.q) {
      const q = f.q.toLowerCase();
      if (
        !(t.description || "").toLowerCase().includes(q) &&
        !(t.city || "").toLowerCase().includes(q)
      )
        return false;
    }
    if (f.dateFrom && t.date && t.date < f.dateFrom) return false;
    if (f.dateTo && t.date && t.date > f.dateTo) return false;
    if (f.amtMin != null && t.amount < f.amtMin) return false;
    if (f.amtMax != null && t.amount > f.amtMax) return false;
    if (f.currency && _txnCurrency(t) !== f.currency.toUpperCase())
      return false;
    return true;
  }

  function _matchesRcptFilter(r, f) {
    if (f.q) {
      const q = f.q.toLowerCase();
      if (!(r.vendor || "").toLowerCase().includes(q)) return false;
    }
    if (f.dateFrom && r.date && r.date < f.dateFrom) return false;
    if (f.dateTo && r.date && r.date > f.dateTo) return false;
    if (f.amtMin != null && r.amount < f.amtMin) return false;
    if (f.amtMax != null && r.amount > f.amtMax) return false;
    if (
      f.currency &&
      (r.currency || "").toUpperCase() !== f.currency.toUpperCase()
    )
      return false;
    return true;
  }

  function _renderTxnList() {
    const host = $("reconTxnList");
    // Two-stage filter: drop matched rows when the toggle is off (lets the
    // user focus on what still needs pairing), then apply the per-pane
    // search / date / amount filters.
    const filtered = reconState.txns
      .filter((t) =>
        reconState.showMatchedTxns ? true : _txnMatches(t).length === 0,
      )
      .filter((t) => _matchesTxnFilter(t, reconState.txnFilter));
    if (!reconState.txns.length) {
      host.innerHTML = `
        <div class="recon-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <h3>Pick a statement to start reconciling</h3>
          <p>Upload a monthly statement on the Statements tab — we'll bring you back here with its transactions parsed.</p>
        </div>`;
      _updateCountChip("reconTxnCount", 0, 0);
      return;
    }
    // Special-case the empty state when EVERYTHING matched and the user is
    // hiding matched rows — celebrate it instead of saying "no results".
    if (
      !filtered.length &&
      !reconState.showMatchedTxns &&
      reconState.txns.every((t) => _txnMatches(t).length > 0)
    ) {
      host.innerHTML = `
        <div class="recon-empty-small recon-all-matched">
          <svg viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          All transactions are matched.
        </div>`;
      _updateCountChip("reconTxnCount", 0, reconState.txns.length);
      return;
    }
    if (!filtered.length) {
      host.innerHTML = `<div class="recon-empty-small">No transactions match your filters.</div>`;
      _updateCountChip("reconTxnCount", 0, reconState.txns.length);
      return;
    }
    host.innerHTML = filtered.map(_renderTxnCard).join("");
    _bindTxnCards();
    _updateCountChip("reconTxnCount", filtered.length, reconState.txns.length);
  }

  // Always read `matches` (array) — the backend keeps `match` populated
  // with the first entry for legacy compatibility but cumulative pairing
  // means we have to walk the list to render every paired receipt.
  function _txnMatches(t) {
    if (Array.isArray(t.matches)) return t.matches;
    return t.match ? [t.match] : [];
  }

  function _renderTxnCard(t) {
    const ms = _txnMatches(t);
    const isMatched = ms.length > 0;
    const cls = ["recon-txn", isMatched ? "matched" : ""].join(" ");
    // International rows: show the native amount as the headline (matches
    // what the user's receipt shows) and the BRL bill total as a smaller
    // secondary line. Domestic rows stay single-line BRL.
    const isIntl =
      t.original_currency &&
      t.original_amount != null &&
      t.original_currency !== "BRL";
    const headlineAmt = isIntl
      ? _fmt.money(t.original_amount, t.original_currency)
      : _fmt.money(t.amount, t.currency || "BRL");
    const subAmt = isIntl
      ? `<span class="recon-amount-sub">${_fmt.money(t.amount, "BRL")}</span>`
      : "";
    // Cumulative-progress chip: when a txn has ≥2 matches, show how
    // much the paired receipts sum to vs the bill total so the user can
    // see "R$ 100 of R$ 120 paired".
    let progressHtml = "";
    if (ms.length >= 2) {
      const sum = ms.reduce(
        (acc, m) => acc + (parseFloat(m.receipt_amount) || 0),
        0,
      );
      const target = isIntl ? t.original_amount : t.amount;
      const cur = isIntl ? t.original_currency : "BRL";
      const close = target != null && Math.abs(sum - target) < 0.5;
      progressHtml = `<span class="recon-cum-progress ${close ? "ok" : ""}">${_fmt.money(sum, cur)} of ${_fmt.money(target, cur)}</span>`;
    }
    return `
      <div class="${cls}" data-index="${t.index}" draggable="false">
        <div class="recon-txn-top">
          <span class="recon-date">${_fmt.date(t.date)}</span>
          <span class="recon-desc" title="${_esc(t.description)}">${_esc(t.description)}</span>
          <span class="recon-amount-stack">
            <span class="recon-amount">${headlineAmt}</span>
            ${subAmt}
          </span>
        </div>
        <div class="recon-txn-bot">
          ${_renderCategoryControl(t, isMatched)}
          ${t.city ? `<span class="recon-city">${_esc(t.city)}</span>` : ""}
          ${isIntl ? `<span class="badge badge-accent recon-cur">${_esc(t.original_currency)}</span>` : ""}
          ${progressHtml}
        </div>
        ${_renderFormFields(t)}
        ${ms.map(_renderMatchTag).join("")}
        ${isMatched ? "" : _renderNoteArea(t)}
      </div>
    `;
  }

  // Report form fields that print on every line of the front page:
  // "Detalhamento da despesa" (label) and "Centro de Custo+Uneg". Both are
  // editable inline and saved per (statement, transaction). Blank cost-center
  // falls back to the profile default on the report.
  function _renderFormFields(t) {
    // Matched rows take their Centro de Custo from the receipt(s) — show it
    // read-only (edit on the Receipts tab). Unmatched rows keep the editable
    // per-line override.
    const cc = t.cost_center_locked
      ? `<span class="recon-cc-chip" title="From the matched receipt — edit it on the Receipts tab">${
          t.cost_center ? _esc(t.cost_center) : "CC: profile default"
        }</span>`
      : `<input class="recon-cc-input" data-index="${t.index}"
          value="${_esc(t.cost_center || "")}" placeholder="C. Custo+Uneg"
          title="Centro de Custo+Uneg (blank = default)" />`;
    return `
      <div class="recon-form-row">
        <input class="recon-detalhe-input" data-index="${t.index}"
          value="${_esc(t.detalhe || "")}" placeholder="Detalhamento"
          title="Report 'Detalhamento da despesa' label" />
        ${cc}
      </div>`;
  }

  // Category control: matched cards show the receipt's category (locked —
  // the receipt wins); unmatched cards get an editable dropdown that drives
  // the report column.
  const _CAT_OPTIONS = [
    ["meals", "Meals"],
    ["transportation", "Transport"],
    ["fuel", "Fuel / Parking"],
    ["travel", "Travel"],
    ["lodging", "Lodging"],
    ["office_supplies", "Office"],
    ["other", "Other"],
  ];

  function _renderCategoryControl(t, isMatched) {
    if (isMatched || t.category_locked) {
      return t.category
        ? `<span class="badge badge-gray" title="From the matched receipt">${_fmt.cat(t.category)}</span>`
        : "";
    }
    const cur = (t.category || "other").toLowerCase();
    const opts = _CAT_OPTIONS
      .map(
        ([v, l]) =>
          `<option value="${v}"${v === cur ? " selected" : ""}>${l}</option>`,
      )
      .join("");
    return `<select class="recon-cat-select" data-index="${t.index}" title="Report category — change to move this charge to another column">${opts}</select>`;
  }

  // Justification editor for open items (unmatched transactions). The note
  // prints on the report in place of the blank "sem comprovante" line.
  function _renderNoteArea(t) {
    if (reconState.editingNote === t.index) {
      return `
        <div class="recon-note-edit" data-index="${t.index}">
          <textarea class="recon-note-input" rows="2"
            placeholder="Why is there no receipt? (lost receipt, unidentified charge…)">${_esc(t.note || "")}</textarea>
          <div class="recon-note-actions">
            <button class="btn btn-ghost btn-sm recon-note-cancel">Cancel</button>
            <button class="btn btn-primary btn-sm recon-note-save" data-index="${t.index}">Save</button>
          </div>
        </div>`;
    }
    if (t.note) {
      return `
        <div class="recon-note-display recon-note-trigger" data-index="${t.index}" title="Click to edit justification">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
          <span>${_esc(t.note)}</span>
        </div>`;
    }
    return `
      <button class="recon-note-add recon-note-trigger" data-index="${t.index}">
        + justification (no receipt)
      </button>`;
  }

  function _renderMatchTag(m) {
    const kindIcon =
      m.kind === "auto"
        ? `<svg class="recon-kind" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4M8 16h.01M16 16h.01"/></svg>`
        : `<svg class="recon-kind" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>`;
    const scoreHtml =
      m.score != null
        ? `<span class="recon-score">${Math.round(m.score * 100)}%</span>`
        : "";
    return `
      <div class="recon-match-tag">
        ${kindIcon}
        <span class="badge badge-green">
          <span class="recon-match-vendor">${_esc(m.receipt_vendor || "receipt")}</span>
          <span class="recon-match-amt">${_fmt.money(m.receipt_amount, m.receipt_currency)}</span>
        </span>
        ${scoreHtml}
        <button class="btn btn-ghost btn-sm recon-unlink" data-match="${m.id}" title="Unlink this match">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>
    `;
  }

  function _renderDropArea() {
    return `
      <div class="recon-drop" data-target="txn">
        <span class="recon-drop-text">Drop a receipt here</span>
      </div>
    `;
  }

  function _renderReceiptList() {
    const host = $("reconReceiptList");
    // Reconcile is corp-card only — reimbursement receipts are personal
    // and never appear on a corp-card statement, so they'd just clutter
    // the right pane without ever being pairable.
    const corp = reconState.receipts.filter(
      (r) => r.expense_type !== "reimbursement",
    );
    let visible = corp.filter((r) =>
      reconState.showMatched ? true : !r.matched,
    );
    visible = visible.filter((r) =>
      _matchesRcptFilter(r, reconState.rcptFilter),
    );

    const corpUnmatched = corp.filter((r) => !r.matched).length;

    if (!corp.length) {
      host.innerHTML = `<div class="recon-empty-small">No corp-card receipts in your library.</div>`;
      _updateCountChip("reconRcptCount", 0, 0);
      return;
    }
    if (!visible.length && !corp.some((r) => !r.matched)) {
      host.innerHTML = `
        <div class="recon-empty-small recon-all-matched">
          <svg viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          All receipts are matched.
        </div>`;
      _updateCountChip("reconRcptCount", 0, 0);
      return;
    }
    if (!visible.length) {
      host.innerHTML = `<div class="recon-empty-small">No receipts match your filters.</div>`;
      _updateCountChip("reconRcptCount", 0, corpUnmatched);
      return;
    }
    host.innerHTML = visible.map(_renderReceiptCard).join("");
    _bindReceiptCards();
    _updateCountChip("reconRcptCount", visible.length, corpUnmatched);
  }

  function _renderReceiptCard(r) {
    const isMatched = r.matched;
    const cls = ["recon-rcpt", isMatched ? "matched" : ""].join(" ");
    // Find the (txn, match) pair for this receipt: any txn whose matches
    // list contains a row pointing at this receipt id. Receipts are still
    // 1:1 — a receipt can't be paired against two transactions.
    let matchedTxn = null;
    let matchOnTxn = null;
    if (isMatched) {
      for (const t of reconState.txns) {
        const m = _txnMatches(t).find((mm) => mm.receipt_id === r.id);
        if (m) {
          matchedTxn = t;
          matchOnTxn = m;
          break;
        }
      }
    }
    // Include the unlink button right next to the "Matched to ..." line
    // so the user can drop a bad pairing without first ticking "Show
    // matched transactions" to find the txn-side × button.
    const matchedLine =
      matchedTxn && matchOnTxn
        ? `<span class="recon-rcpt-matched-line">
           <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
           Matched to <strong>${_esc(matchedTxn.description)}</strong>
           <button class="btn btn-ghost btn-sm recon-unlink recon-rcpt-unlink" data-match="${matchOnTxn.id}" title="Unlink this match">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
           </button>
         </span>`
        : "";
    return `
      <div class="${cls}" data-rid="${r.id}" draggable="${!isMatched}">
        <div class="recon-rcpt-top">
          <span class="recon-date">${_fmt.date(r.date)}</span>
          <span class="recon-desc" title="${_esc(r.vendor)}">${_esc(r.vendor)}</span>
          <span class="recon-amount">${_fmt.money(r.amount, r.currency)}</span>
        </div>
        <div class="recon-rcpt-bot">
          ${_renderThumb(r)}
          ${r.category ? `<span class="badge badge-gray">${_fmt.cat(r.category)}</span>` : ""}
          ${r.currency ? `<span class="badge badge-accent recon-cur">${_esc(r.currency)}</span>` : ""}
          ${matchedLine}
          ${!isMatched ? `<span class="recon-grip" aria-hidden="true"></span>` : ""}
        </div>
      </div>
    `;
  }

  // Pick the right thumbnail strategy based on file extension. Mirrors the
  // logic in `renderReceipts` for consistency — images use the static mount,
  // HEIC routes through /api/preview/ (server-side conversion), PDFs get a
  // small badge (browsers can't render PDF as an <img>), uber_ synth rows
  // get a car-glyph placeholder, and anything else falls back to the
  // striped CSS placeholder.
  function _renderThumb(r) {
    const name = r.filename || "";
    const ext = name.split(".").pop().toLowerCase();
    const viewable = ["jpg", "jpeg", "png", "gif", "webp"].includes(ext);
    const isHeic = ext === "heic" || ext === "heif";
    const isPdf = name.toLowerCase().endsWith(".pdf");
    const isUber = name.startsWith("uber_");
    const enc = encodeURIComponent(name);
    // Lightbox needs the URL the user can actually see; for HEIC that's the
    // preview URL (browser can't render the raw file), for everything else
    // the static mount is fine.
    const lbSrc = isHeic ? `/api/preview/${enc}` : `/receipts/${name}`;
    const clickAttr =
      !isUber && name
        ? ` onclick="event.stopPropagation(); openLightbox('${lbSrc}', ${r.id})"`
        : "";

    if (viewable) {
      return `<div class="recon-thumb"${clickAttr}><img src="/receipts/${name}" loading="lazy" alt="" onerror="this.parentNode.classList.add('recon-thumb-fail'); this.remove()"></div>`;
    }
    if (isHeic) {
      return `<div class="recon-thumb"${clickAttr}><img src="/api/preview/${enc}" loading="lazy" alt="" onerror="this.parentNode.classList.add('recon-thumb-fail'); this.remove()"></div>`;
    }
    if (isPdf) {
      // Open in a new tab via the preview route (Content-Type: application/pdf).
      return `<a class="recon-thumb recon-thumb-pdf" href="/api/preview/${enc}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open receipt PDF"><span>PDF</span></a>`;
    }
    if (isUber) {
      return `<div class="recon-thumb recon-thumb-uber" title="Imported from Uber CSV"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="17" r="2"/><circle cx="17" cy="17" r="2"/><path d="M5 17H3v-6l2-5h11l4 5v6h-2"/><path d="M5 11h14"/></svg></div>`;
    }
    return `<div class="recon-thumb"></div>`;
  }

  // ── Bindings ─────────────────────────────────────────────────────────
  function _bindHeader() {
    $("reconStatement").addEventListener("change", (e) => {
      if (e.target.value) _selectStatement(+e.target.value);
    });
    $("reconAutoBtn").addEventListener("click", _onAutoMatch);
    $("reconShowMatched").addEventListener("change", (e) => {
      reconState.showMatched = e.target.checked;
      _renderReceiptList();
    });
    $("reconShowMatchedTxns").addEventListener("change", (e) => {
      reconState.showMatchedTxns = e.target.checked;
      _renderTxnList();
    });
  }

  function _bindFilters() {
    const fields = [
      ["reconTxnQ", "q", "txnFilter", "rt.recon.txnFilter", false],
      ["reconTxnFrom", "dateFrom", "txnFilter", "rt.recon.txnFilter", false],
      ["reconTxnTo", "dateTo", "txnFilter", "rt.recon.txnFilter", false],
      ["reconTxnMin", "amtMin", "txnFilter", "rt.recon.txnFilter", true],
      ["reconTxnMax", "amtMax", "txnFilter", "rt.recon.txnFilter", true],
      ["reconRcptQ", "q", "rcptFilter", "rt.recon.receiptFilter", false],
      [
        "reconRcptFrom",
        "dateFrom",
        "rcptFilter",
        "rt.recon.receiptFilter",
        false,
      ],
      ["reconRcptTo", "dateTo", "rcptFilter", "rt.recon.receiptFilter", false],
      ["reconRcptMin", "amtMin", "rcptFilter", "rt.recon.receiptFilter", true],
      ["reconRcptMax", "amtMax", "rcptFilter", "rt.recon.receiptFilter", true],
      [
        "reconTxnCurrency",
        "currency",
        "txnFilter",
        "rt.recon.txnFilter",
        false,
      ],
      [
        "reconRcptCurrency",
        "currency",
        "rcptFilter",
        "rt.recon.receiptFilter",
        false,
      ],
    ];
    fields.forEach(([id, key, stateKey, storeKey, isNum]) => {
      const el = $(id);
      if (!el) return;
      const cur = reconState[stateKey][key];
      el.value = cur == null ? "" : cur;
      // `change` fires on <select>, `input` fires on text/number/date. Bind
      // both so this loop covers every control without a per-field switch.
      const evt = el.tagName === "SELECT" ? "change" : "input";
      el.addEventListener(evt, () => {
        let v = el.value;
        if (v === "") v = null;
        else if (isNum) v = parseFloat(v);
        reconState[stateKey][key] = v;
        _saveFilter(storeKey, reconState[stateKey]);
        if (stateKey === "txnFilter") _renderTxnList();
        else _renderReceiptList();
      });
    });
  }

  // Populate the two currency dropdowns from whatever currencies are
  // actually present in the data. Re-runs after `_refreshAll` so a newly
  // ingested international row makes its currency appear immediately.
  function _populateCurrencyDropdowns() {
    const txnCcys = new Set();
    reconState.txns.forEach((t) => txnCcys.add(_txnCurrency(t)));
    const rcptCcys = new Set();
    reconState.receipts.forEach((r) => {
      const c = (r.currency || "").toUpperCase();
      if (c) rcptCcys.add(c);
    });

    const sync = (sel, set, key, stateKey) => {
      if (!sel) return;
      const current = reconState[stateKey][key] || "";
      const want = new Set(set);
      // Drop options that no longer apply (except the empty "Any ccy").
      [...sel.options].forEach((o) => {
        if (o.value && !want.has(o.value)) sel.removeChild(o);
        else if (o.value) want.delete(o.value);
      });
      // Add any new currencies, sorted alphabetically.
      [...want].sort().forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        sel.appendChild(opt);
      });
      // Restore selection if it still exists.
      if (current && [...sel.options].some((o) => o.value === current)) {
        sel.value = current;
      } else if (current) {
        // Stored currency no longer present — clear the filter.
        reconState[stateKey][key] = null;
        sel.value = "";
      }
    };

    sync($("reconTxnCurrency"), txnCcys, "currency", "txnFilter");
    sync($("reconRcptCurrency"), rcptCcys, "currency", "rcptFilter");
  }

  function _bindReceiptCards() {
    document.querySelectorAll(".recon-rcpt").forEach((el) => {
      if (el.classList.contains("matched")) return;
      el.addEventListener("dragstart", _onDragStart);
      el.addEventListener("dragend", _onDragEnd);
    });
    // Unlink buttons live INSIDE matched receipt cards. Bind them here
    // (scoped to the receipt list) because `_bindTxnCards` runs before
    // the receipt list re-renders — its document-wide query would miss
    // the receipt-side buttons that hadn't been inserted yet.
    document
      .querySelectorAll("#reconReceiptList .recon-rcpt-unlink")
      .forEach((el) => {
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          _onUnlink(+el.dataset.match);
        });
      });
  }

  function _bindTxnCards() {
    // Every txn card is a drop target — matched txns included, since
    // multiple receipts can now cumulatively pair to one transaction.
    document.querySelectorAll(".recon-txn").forEach((el) => {
      el.addEventListener("dragover", _onDragOver);
      el.addEventListener("dragenter", _onDragEnter);
      el.addEventListener("dragleave", _onDragLeave);
      el.addEventListener("drop", _onDrop);
    });
    document.querySelectorAll(".recon-unlink").forEach((el) => {
      el.addEventListener("click", () => _onUnlink(+el.dataset.match));
    });
    // Justification editor (open items).
    document.querySelectorAll(".recon-note-trigger").forEach((el) => {
      el.addEventListener("click", () => {
        reconState.editingNote = +el.dataset.index;
        _renderTxnList();
        const ta = document.querySelector(".recon-note-input");
        if (ta) {
          ta.focus();
          ta.setSelectionRange(ta.value.length, ta.value.length);
        }
      });
    });
    document.querySelectorAll(".recon-note-cancel").forEach((el) => {
      el.addEventListener("click", () => {
        reconState.editingNote = null;
        _renderTxnList();
      });
    });
    document.querySelectorAll(".recon-note-save").forEach((el) => {
      el.addEventListener("click", () => _saveNote(+el.dataset.index));
    });
    // Save on Cmd/Ctrl+Enter; cancel on Escape.
    document.querySelectorAll(".recon-note-input").forEach((ta) => {
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          _saveNote(reconState.editingNote);
        } else if (e.key === "Escape") {
          reconState.editingNote = null;
          _renderTxnList();
        }
      });
    });
    // Category override on unmatched cards.
    document.querySelectorAll(".recon-cat-select").forEach((sel) => {
      // Don't let a click on the select start a card drag / drop interaction.
      sel.addEventListener("mousedown", (e) => e.stopPropagation());
      sel.addEventListener("change", () =>
        _saveCategory(+sel.dataset.index, sel.value),
      );
    });
    // Detalhamento + Centro de Custo inline edits (save on change/blur).
    document.querySelectorAll(".recon-detalhe-input").forEach((inp) => {
      inp.addEventListener("mousedown", (e) => e.stopPropagation());
      inp.addEventListener("change", () =>
        _saveDetalhe(+inp.dataset.index, inp.value),
      );
    });
    document.querySelectorAll(".recon-cc-input").forEach((inp) => {
      inp.addEventListener("mousedown", (e) => e.stopPropagation());
      inp.addEventListener("change", () =>
        _saveCostCenter(+inp.dataset.index, inp.value),
      );
    });
  }

  async function _saveCategory(txnIndex, category) {
    const txn = reconState.txns.find((t) => t.index === txnIndex);
    if (txn) txn.category = category; // optimistic
    try {
      await api(
        "PUT",
        `/api/statements/${reconState.statementId}/transactions/${txnIndex}/category`,
        { category },
      );
    } catch (e) {
      _flash("err", `Couldn't save category: ${e.message}`);
    }
  }

  async function _saveDetalhe(txnIndex, label) {
    const txn = reconState.txns.find((t) => t.index === txnIndex);
    if (txn) txn.detalhe = label; // optimistic
    try {
      await api(
        "PUT",
        `/api/statements/${reconState.statementId}/transactions/${txnIndex}/detalhe`,
        { detalhe: label },
      );
    } catch (e) {
      _flash("err", `Couldn't save detalhamento: ${e.message}`);
    }
  }

  async function _saveCostCenter(txnIndex, value) {
    const txn = reconState.txns.find((t) => t.index === txnIndex);
    if (txn) txn.cost_center = value; // optimistic
    try {
      await api(
        "PUT",
        `/api/statements/${reconState.statementId}/transactions/${txnIndex}/cost-center`,
        { cost_center: value },
      );
    } catch (e) {
      _flash("err", `Couldn't save cost center: ${e.message}`);
    }
  }

  async function _saveNote(txnIndex) {
    const ta = document.querySelector(".recon-note-input");
    const text = ta ? ta.value.trim() : "";
    const txn = reconState.txns.find((t) => t.index === txnIndex);
    if (txn) txn.note = text; // optimistic
    reconState.editingNote = null;
    _renderTxnList();
    try {
      await api(
        "PUT",
        `/api/statements/${reconState.statementId}/transactions/${txnIndex}/note`,
        { note: text },
      );
    } catch (e) {
      _flash("err", `Couldn't save note: ${e.message}`);
    }
  }

  function _onDragStart(e) {
    const id = +e.currentTarget.dataset.rid;
    const receipt = reconState.receipts.find((r) => r.id === id);
    if (!receipt || receipt.matched) {
      e.preventDefault();
      return;
    }
    reconState.dragging = receipt;
    e.currentTarget.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try {
      e.dataTransfer.setData("text/plain", String(id));
    } catch {}
    document.body.classList.add("recon-drag-in-flight");
    // Decorate ALL txn cards (including matched ones — cumulative drops
    // are now allowed). Smart-match still considers good fit by amount
    // or by cumulative-fill of an already-matched txn.
    document.querySelectorAll(".recon-txn").forEach((tEl) => {
      tEl.classList.add("droppable");
      const txn = reconState.txns.find((t) => t.index === +tEl.dataset.index);
      if (txn && _isGoodMatch(receipt, txn)) tEl.classList.add("good-match");
    });
  }

  function _onDragEnd(e) {
    e.currentTarget.classList.remove("dragging");
    reconState.dragging = null;
    document.body.classList.remove("recon-drag-in-flight");
    document.querySelectorAll(".recon-txn").forEach((t) => {
      t.classList.remove("droppable", "good-match", "drop-hover");
    });
  }

  function _onDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }
  function _onDragEnter(e) {
    e.currentTarget.classList.add("drop-hover");
  }
  function _onDragLeave(e) {
    if (e.currentTarget.contains(e.relatedTarget)) return;
    e.currentTarget.classList.remove("drop-hover");
  }

  async function _onDrop(e) {
    e.preventDefault();
    const txnEl = e.currentTarget;
    txnEl.classList.remove("drop-hover");
    const txnIndex = +txnEl.dataset.index;
    const receipt = reconState.dragging;
    if (!receipt) return;
    await _pair(receipt.id, txnIndex);
  }

  async function _pair(receiptId, txnIndex) {
    const receipt = reconState.receipts.find((r) => r.id === receiptId);
    const txn = reconState.txns.find((t) => t.index === txnIndex);
    if (!receipt || !txn) return;
    if (receipt.matched) return; // receipt is still 1:1

    // Optimistic update — append to the cumulative matches array. We use
    // a negative placeholder id; the API response replaces it.
    if (!Array.isArray(txn.matches)) txn.matches = txn.match ? [txn.match] : [];
    const optimistic = {
      id: -Math.floor(Math.random() * 1e6) - 1,
      receipt_id: receipt.id,
      receipt_vendor: receipt.vendor,
      receipt_amount: receipt.amount,
      receipt_currency: receipt.currency,
      score: null,
      kind: "manual",
    };
    const snapshot = {
      matched: receipt.matched,
      matches: [...txn.matches],
      match: txn.match,
    };
    receipt.matched = true;
    txn.matches.push(optimistic);
    txn.match = txn.matches[0];
    _render();
    _flash("ok", `Matched ${receipt.vendor} → ${txn.description}`);

    try {
      const res = await api("POST", "/api/matches", {
        receipt_id: receipt.id,
        statement_id: reconState.statementId,
        transaction_index: txnIndex,
      });
      // Replace the placeholder id with the real one.
      const slot = txn.matches.find((m) => m.id === optimistic.id);
      if (slot) slot.id = res.id;
      if (txn.match === optimistic) txn.match = slot;
    } catch (e) {
      receipt.matched = snapshot.matched;
      txn.matches = snapshot.matches;
      txn.match = snapshot.match;
      _render();
      _flash("err", `Failed to save match: ${e.message}`);
    }
    // Refresh the global stats / receipts table so the change shows up there too.
    if (typeof loadStats === "function") loadStats();
  }

  async function _onUnlink(matchId) {
    // Find the txn + the specific match entry inside its matches array.
    let txn = null;
    let matchEntry = null;
    for (const t of reconState.txns) {
      const ms = _txnMatches(t);
      const m = ms.find((mm) => mm.id === matchId);
      if (m) {
        txn = t;
        matchEntry = m;
        break;
      }
    }
    if (!txn || !matchEntry) return;
    const receiptId = matchEntry.receipt_id;
    const receipt = reconState.receipts.find((r) => r.id === receiptId);

    const snapshot = {
      matches: Array.isArray(txn.matches) ? [...txn.matches] : [],
      match: txn.match,
      matched: receipt ? receipt.matched : null,
    };
    if (Array.isArray(txn.matches)) {
      txn.matches = txn.matches.filter((m) => m.id !== matchId);
    } else {
      txn.matches = [];
    }
    txn.match = txn.matches[0] || null;
    if (receipt) receipt.matched = false;
    _render();
    _flash("ok", `Unlinked from ${txn.description}`);

    try {
      await api("DELETE", `/api/matches/${matchId}`);
    } catch (e) {
      txn.matches = snapshot.matches;
      txn.match = snapshot.match;
      if (receipt && snapshot.matched != null)
        receipt.matched = snapshot.matched;
      _render();
      _flash("err", `Failed to unlink: ${e.message}`);
    }
    if (typeof loadStats === "function") loadStats();
  }

  async function _onAutoMatch() {
    if (reconState.autoMatchRunning) return;
    reconState.autoMatchRunning = true;
    const btn = $("reconAutoBtn");
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> Matching…`;
    try {
      const res = await api("POST", "/api/match");
      if (res.success) {
        // Re-pull the statement transactions to pick up the new auto-matches
        // (the engine wrote them straight to DB).
        if (reconState.statementId != null) {
          await _selectStatement(reconState.statementId);
        }
        // Refresh receipt match flags.
        await _refreshReceiptsOnly();
        _flash(
          "ok",
          `Auto-matched ${res.total_matched} of ${res.total_matched + res.total_unmatched_receipts} receipt${res.total_matched === 1 ? "" : "s"}.`,
        );
      } else {
        _flash("err", res.message || "Auto-match returned no result.");
      }
    } catch (e) {
      _flash("err", `Auto-match failed: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
      reconState.autoMatchRunning = false;
      if (typeof loadStats === "function") loadStats();
    }
  }

  async function _refreshReceiptsOnly() {
    try {
      const rcpts = await api("GET", "/api/receipts");
      reconState.receipts = (rcpts.receipts || []).map((r) => {
        const m = (() => {
          try {
            return JSON.parse(r.metadata || "{}");
          } catch {
            return {};
          }
        })();
        return {
          id: r.id,
          filename: r.filename || "",
          vendor: m.vendor_name || "Unknown",
          amount: m.total_amount != null ? parseFloat(m.total_amount) : null,
          currency: m.currency || null,
          date: m.date || (r.created_at || "").slice(0, 10),
          category: m.category || null,
          matched: r.match_status === "matched",
          expense_type: r.expense_type || "corp_card",
        };
      });
      reconState.receipts.sort((a, b) =>
        (a.date || "").localeCompare(b.date || ""),
      );
      _renderReceiptList();
    } catch {}
  }

  function _updateCountChip(elId, shown, total) {
    const el = $(elId);
    if (!el) return;
    el.textContent = shown === total ? `${total}` : `${shown} of ${total}`;
  }

  let _flashTimer = null;
  function _flash(kind, msg) {
    const el = $("reconFlash");
    if (!el) return;
    el.className = `status-msg ${kind}`;
    el.textContent = msg;
    clearTimeout(_flashTimer);
    _flashTimer = setTimeout(() => {
      el.textContent = "";
      el.className = "status-msg";
    }, 3000);
  }

  // Re-fetch the current statement's transactions + re-render. Used after
  // the Review-&-edit modal persists changes so the board reflects them.
  async function reload() {
    if (reconState.statementId != null)
      await _selectStatement(reconState.statementId);
  }

  return { init, reload, state: reconState };
})();

// ─── Users (cardholder roster) ─────────────────────────────────────────────
// (Reference list of per-user profile fields; the form collects from the
// DOM, so this isn't load-bearing — kept in sync for clarity. GL accounts
// live in the global config, not here.)
const _USER_FIELDS = [
  "nome",
  "cost_center",
  "centro_custo",
  "motivo",
  "local",
  "projeto",
  "expense_kind",
  "moeda",
  "favorecido",
  "banco",
  "agencia",
  "conta_corrente",
  "cpf_rg",
];
let _editingUserId = null;
let _usersCache = [];

// ─── Accounting accounts (company-wide GL accounts, global config) ──────────
async function openAccountsModal() {
  $("accountsModal").classList.add("open");
  $("accountsStatus").textContent = "";
  try {
    const profile = await api("GET", "/api/report-profile");
    document.querySelectorAll("#accountsForm [data-k]").forEach((el) => {
      el.value = profile[el.dataset.k] ?? "";
    });
  } catch (e) {
    $("accountsStatus").className = "status-msg err";
    $("accountsStatus").textContent = "Couldn't load: " + e.message;
  }
}

function closeAccountsModal() {
  $("accountsModal").classList.remove("open");
}

async function _saveAccounts() {
  const btn = $("accountsSaveBtn");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Saving…';
  try {
    const patch = {};
    document.querySelectorAll("#accountsForm [data-k]").forEach((el) => {
      patch[el.dataset.k] = el.value;
    });
    await api("PUT", "/api/report-profile", patch);
    $("accountsStatus").className = "status-msg ok";
    $("accountsStatus").textContent = "Saved.";
    setTimeout(closeAccountsModal, 800);
  } catch (e) {
    $("accountsStatus").className = "status-msg err";
    $("accountsStatus").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

(function _wireAccountsModal() {
  const open = $("accountsBtn");
  if (open) open.addEventListener("click", openAccountsModal);
  const save = $("accountsSaveBtn");
  if (save) save.addEventListener("click", _saveAccounts);
  // No backdrop-click-to-close: an accidental outside click shouldn't
  // discard a half-filled form. Dismiss via Cancel/Save.
})();

async function loadUsers() {
  try {
    const { users } = await api("GET", "/api/report-users");
    _usersCache = users || [];
  } catch {
    _usersCache = [];
  }
  $("navUserCount").textContent = _usersCache.length;
  const host = $("usersList");
  if (!host) return;
  if (!_usersCache.length) {
    host.innerHTML =
      '<div class="users-empty">No users yet. Add a cardholder so reports auto-fill their name, cost center and bank details.</div>';
    return;
  }
  host.innerHTML = _usersCache
    .map((u) => {
      const sub = [u.cost_center && `C/C ${u.cost_center}`, u.local, u.banco]
        .filter(Boolean)
        .join(" · ");
      return `<div class="user-row">
        <div class="user-id">#${u.id}</div>
        <div class="user-main">
          <div class="user-name">${_esc(u.nome || "(no name)")}</div>
          <div class="user-meta">${_esc(sub || "—")}</div>
        </div>
        <button class="btn btn-outline btn-sm" onclick="openUserModal(${u.id})">Edit</button>
        <button class="btn btn-ghost btn-sm" onclick="deleteUser(${u.id})">Del</button>
      </div>`;
    })
    .join("");
}

async function openUserModal(uid) {
  _editingUserId = uid ?? null;
  $("userModalTitle").textContent = uid != null ? "Edit user" : "Add user";
  $("userModalStatus").textContent = "";
  const u = uid != null ? _usersCache.find((x) => x.id === uid) || {} : {};
  // Pre-fill from an existing user, or from the global default template.
  let defaults = {};
  if (uid == null) {
    try {
      defaults = await api("GET", "/api/report-profile");
    } catch {}
  }
  const src = uid != null ? u : defaults;
  document.querySelectorAll("#userForm [data-k]").forEach((el) => {
    const k = el.dataset.k;
    el.value = src[k] ?? "";
  });
  $("userModal").classList.add("open");
}

function closeUserModal() {
  $("userModal").classList.remove("open");
  _editingUserId = null;
}

function _collectUser() {
  const patch = {};
  document.querySelectorAll("#userForm [data-k]").forEach((el) => {
    patch[el.dataset.k] = el.value;
  });
  return patch;
}

async function _saveUser() {
  const btn = $("userSaveBtn");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Saving…';
  try {
    const body = _collectUser();
    if (!body.nome || !body.nome.trim()) {
      throw new Error("Nome is required");
    }
    if (_editingUserId != null) {
      await api("PUT", `/api/report-users/${_editingUserId}`, body);
    } else {
      await api("POST", "/api/report-users", body);
    }
    closeUserModal();
    loadUsers();
  } catch (e) {
    $("userModalStatus").className = "status-msg err";
    $("userModalStatus").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

async function deleteUser(uid) {
  if (!confirm("Remove this user?")) return;
  try {
    await api("DELETE", `/api/report-users/${uid}`);
    loadUsers();
  } catch (e) {
    alert("Failed to delete: " + e.message);
  }
}

(function _wireUsers() {
  const add = $("userAddBtn");
  if (add) add.addEventListener("click", () => openUserModal(null));
  const save = $("userSaveBtn");
  if (save) save.addEventListener("click", _saveUser);
  // No backdrop-click-to-close (avoid discarding a half-filled form).
})();

// ─── Expense report (PDF) ──────────────────────────────────────────────────
// "Generate report" on the reconcile header → pick the cardholder (auto-
// guessed from the statement) → download the Prestação de Contas PDF.

async function openReportModal() {
  const sid = Reconcile.state && Reconcile.state.statementId;
  if (sid == null) {
    alert("Pick a statement on the Reconcile tab first.");
    return;
  }
  $("reportModal").classList.add("open");
  $("reportStatus").textContent = "";
  $("reportGuessHint").textContent = "";
  const sel = $("reportUserSelect");
  // Populate the picker from the roster.
  try {
    const { users } = await api("GET", "/api/report-users");
    _usersCache = users || [];
  } catch {
    _usersCache = [];
  }
  if (!_usersCache.length) {
    sel.innerHTML =
      '<option value="">(no users — using saved defaults)</option>';
  } else {
    sel.innerHTML =
      '<option value="">(saved defaults)</option>' +
      _usersCache
        .map(
          (u) =>
            `<option value="${u.id}">${_esc(u.nome || "#" + u.id)}</option>`,
        )
        .join("");
  }
  // Auto-guess the cardholder from the statement name.
  try {
    const g = await api("GET", `/api/statements/${sid}/guess-user`);
    if (g.user_id != null) {
      sel.value = String(g.user_id);
      $("reportGuessHint").textContent = `Auto-detected: ${g.nome}`;
    } else if (_usersCache.length) {
      $("reportGuessHint").textContent =
        "Couldn't match a user to this statement — pick one.";
    }
  } catch {}
}

function closeReportModal() {
  $("reportModal").classList.remove("open");
}

// Fetch + trigger download of the statement report. `fmt` is "pdf" (combined
// PDF) or "xlsx" (front-page spreadsheet). Shared by the report modal and the
// Review-&-edit modal.
async function _downloadReport(sid, uid, venc, fmt = "pdf") {
  const params = new URLSearchParams();
  if (uid) params.set("user_id", uid);
  if (venc) params.set("vencimento", venc);
  const qs = params.toString() ? `?${params}` : "";
  // Normalise: anything not "xlsx" is the PDF (guards against a stray event
  // object slipping in as `fmt`).
  const ext = fmt === "xlsx" ? "xlsx" : "pdf";
  const path = ext === "xlsx" ? "report.xlsx" : "report";
  const res = await fetch(`/api/statements/${sid}/${path}${qs}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `prestacao_contas_statement_${sid}.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function _generateReport(fmt = "pdf") {
  const sid = Reconcile.state && Reconcile.state.statementId;
  if (sid == null) return;
  const xlsx = fmt === "xlsx";
  const btn = $(xlsx ? "reportXlsxBtn" : "reportGenBtn");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> Building ${xlsx ? "XLSX" : "PDF"}…`;
  $("reportStatus").className = "status-msg";
  $("reportStatus").textContent = xlsx
    ? "Building spreadsheet…"
    : "Rendering receipts — this can take a moment…";
  try {
    const uid = $("reportUserSelect").value;
    const venc = $("reportVencimento") ? $("reportVencimento").value : "";
    await _downloadReport(sid, uid, venc, fmt);
    $("reportStatus").className = "status-msg ok";
    $("reportStatus").textContent =
      `${xlsx ? "Spreadsheet" : "Report"} downloaded.`;
    if (!xlsx) loadReportHistory().catch(() => {});
    setTimeout(closeReportModal, 1200);
  } catch (e) {
    $("reportStatus").className = "status-msg err";
    $("reportStatus").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

(function _wireReportModal() {
  const open = $("reconReportBtn");
  if (open) open.addEventListener("click", openReportModal);
  const xlsx = $("reportXlsxBtn");
  if (xlsx) xlsx.addEventListener("click", () => _generateReport("xlsx"));
  const gen = $("reportGenBtn");
  if (gen) gen.addEventListener("click", () => _generateReport("pdf"));
  // No backdrop-click-to-close (avoid discarding a half-filled form).
})();

// ─── Review & edit report (editable table before PDF export) ────────────────
// Loads the assembled report rows, lets you edit Detalhamento / Centro de
// Custo / column inline (persisted via the same stores the board uses), then
// exports the PDF — which re-derives the same values, so edits flow through.
const _reportEdit = { sid: null, uid: "", venc: "", view: null };
const _COL_TO_CAT = {
  alimentacao: "meals",
  abastecimento: "transportation",
  hospedagem: "lodging",
  outros: "other",
};

function _brl(n) {
  if (n == null || isNaN(n)) return "";
  return (
    "R$ " +
    Number(n).toLocaleString("pt-BR", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

function _reportEditStatus(msg, kind) {
  const el = $("reportEditStatus");
  el.textContent = msg || "";
  el.className = "status-msg" + (kind ? " " + kind : "");
}

function closeReportEditModal() {
  $("reportEditModal").classList.remove("open");
  // Reflect any persisted edits on the Reconcile board.
  if (
    _reportEdit.dirty &&
    typeof Reconcile !== "undefined" &&
    typeof Reconcile.reload === "function"
  ) {
    Reconcile.reload().catch(() => {});
  }
  _reportEdit.dirty = false;
}

async function openReportEditModal() {
  const sid = Reconcile.state && Reconcile.state.statementId;
  if (sid == null) {
    alert("Pick a statement on the Reconcile tab first.");
    return;
  }
  _reportEdit.sid = sid;
  _reportEdit.uid = $("reportUserSelect").value || "";
  _reportEdit.venc = $("reportVencimento") ? $("reportVencimento").value : "";
  _reportEditStatus("");
  $("reportEditBody").innerHTML =
    '<tr><td colspan="6" class="re-loading">Loading…</td></tr>';
  $("reportEditModal").classList.add("open");
  await _loadReportEdit();
}

async function _loadReportEdit() {
  const { sid, uid, venc } = _reportEdit;
  const params = new URLSearchParams();
  if (uid) params.set("user_id", uid);
  if (venc) params.set("vencimento", venc);
  const qs = params.toString() ? `?${params}` : "";
  try {
    const view = await api("GET", `/api/statements/${sid}/report-rows${qs}`);
    _reportEdit.view = view;
    _renderReportEdit(view);
  } catch (e) {
    $("reportEditBody").innerHTML =
      `<tr><td colspan="6" class="re-loading">Failed to load: ${_esc(
        e.message,
      )}</td></tr>`;
  }
}

function _renderReportEdit(view) {
  const colOpts = (sel) =>
    view.columns
      .map(
        (c) =>
          `<option value="${c.key}"${c.key === sel ? " selected" : ""}>${_esc(
            c.label,
          )}</option>`,
      )
      .join("");
  $("reportEditBody").innerHTML = view.rows
    .map(
      (r) => `
      <tr data-index="${r.index}" data-matched="${r.matched ? 1 : 0}" data-receipts="${(
        r.receipt_ids || []
      ).join(",")}">
        <td class="re-date">${_esc(r.date)}</td>
        <td><input class="re-cc" value="${_esc(r.cost_center)}" placeholder="${_esc(
          r.cost_center_effective || "padrão",
        )}" title="${r.matched ? "Saved on the matched receipt" : "Per-line override"}" /></td>
        <td class="re-conta">${_esc(r.conta)}</td>
        <td><input class="re-detalhe" value="${_esc(r.detalhe)}" /></td>
        <td><select class="re-col">${colOpts(r.col)}</select></td>
        <td class="num re-amt">${_brl(r.amount)}</td>
      </tr>`,
    )
    .join("");
  const breakdown = view.columns
    .map((c) => `${c.label.split(" ")[0]} ${_brl(view.totals[c.key] || 0)}`)
    .join(" · ");
  $("reportEditFoot").innerHTML = `
    <tr class="re-tot">
      <td colspan="5">${breakdown}${
        view.iof ? ` · IOF ${_brl(view.iof)}` : ""
      }</td>
      <td class="num">${_brl(view.grand_total)}</td>
    </tr>`;
  _wireReportEditCells();
}

function _wireReportEditCells() {
  document
    .querySelectorAll("#reportEditBody .re-detalhe")
    .forEach((inp) =>
      inp.addEventListener("change", () =>
        _reportEditSave(inp, "detalhe", inp.value),
      ),
    );
  document
    .querySelectorAll("#reportEditBody .re-cc")
    .forEach((inp) =>
      inp.addEventListener("change", () =>
        _reportEditSave(inp, "cost_center", inp.value),
      ),
    );
  document
    .querySelectorAll("#reportEditBody .re-col")
    .forEach((sel) =>
      sel.addEventListener("change", () =>
        _reportEditSave(sel, "column", sel.value),
      ),
    );
}

async function _reportEditSave(el, kind, value) {
  const tr = el.closest("tr");
  const idx = +tr.dataset.index;
  const matched = tr.dataset.matched === "1";
  const receiptIds = (tr.dataset.receipts || "")
    .split(",")
    .filter(Boolean)
    .map(Number);
  const sid = _reportEdit.sid;
  _reportEditStatus("Saving…");
  try {
    if (kind === "detalhe") {
      await api("PUT", `/api/statements/${sid}/transactions/${idx}/detalhe`, {
        detalhe: value,
      });
    } else if (kind === "cost_center") {
      if (matched && receiptIds.length) {
        // Matched line → the receipt(s) drive the CC. Set all of them.
        await Promise.all(
          receiptIds.map((rid) =>
            api("PATCH", `/api/receipts/${rid}`, {
              cost_centers: value || null,
            }),
          ),
        );
      } else {
        await api(
          "PUT",
          `/api/statements/${sid}/transactions/${idx}/cost-center`,
          { cost_center: value },
        );
      }
    } else if (kind === "column") {
      const cat = _COL_TO_CAT[value] || "other";
      if (matched && receiptIds.length) {
        await Promise.all(
          receiptIds.map((rid) =>
            api("PATCH", `/api/receipts/${rid}`, { category: cat }),
          ),
        );
      } else {
        await api(
          "PUT",
          `/api/statements/${sid}/transactions/${idx}/category`,
          { category: cat },
        );
      }
      await _loadReportEdit(); // column shift changes Conta + totals
    }
    _reportEdit.dirty = true; // board reloads on close to reflect this
    _reportEditStatus("Saved.", "ok");
  } catch (e) {
    _reportEditStatus("Failed: " + e.message, "err");
  }
}

async function _reportEditExport(fmt = "pdf") {
  const xlsx = fmt === "xlsx";
  const btn = $(xlsx ? "reportEditXlsxBtn" : "reportEditExportBtn");
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> Building ${xlsx ? "XLSX" : "PDF"}…`;
  _reportEditStatus(
    xlsx ? "Building spreadsheet…" : "Rendering — this can take a moment…",
  );
  try {
    await _downloadReport(
      _reportEdit.sid,
      _reportEdit.uid,
      _reportEdit.venc,
      fmt,
    );
    _reportEditStatus(`${xlsx ? "Spreadsheet" : "Report"} downloaded.`, "ok");
    if (!xlsx) loadReportHistory().catch(() => {});
  } catch (e) {
    _reportEditStatus("Failed: " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

(function _wireReportEditModal() {
  const review = $("reportReviewBtn");
  if (review)
    review.addEventListener("click", () => {
      closeReportModal();
      openReportEditModal();
    });
  const exp = $("reportEditExportBtn");
  if (exp) exp.addEventListener("click", () => _reportEditExport("pdf"));
  const expx = $("reportEditXlsxBtn");
  if (expx) expx.addEventListener("click", () => _reportEditExport("xlsx"));
})();

// ─── Reimbursement report (receipts only, no statement) ─────────────────────
// "Reimbursement report" on the Receipts tab → pick cardholder + currency
// (+ optional dates) → download the form built from reimbursement receipts.
let _reimbReceipts = []; // cached reimbursement-type receipts (date, currency)

async function openReimbModal() {
  $("reimbModal").classList.add("open");
  $("reimbStatus").textContent = "";

  // Cardholder picker from the roster.
  const usel = $("reimbUserSelect");
  try {
    const { users } = await api("GET", "/api/report-users");
    _usersCache = users || [];
  } catch {
    _usersCache = [];
  }
  usel.innerHTML =
    '<option value="">(saved defaults)</option>' +
    _usersCache
      .map(
        (u) => `<option value="${u.id}">${_esc(u.nome || "#" + u.id)}</option>`,
      )
      .join("");

  // Pull reimbursement receipts to derive currencies + count.
  try {
    const { receipts } = await api("GET", "/api/receipts");
    _reimbReceipts = (receipts || [])
      .filter((r) => (r.expense_type || "corp_card") === "reimbursement")
      .map((r) => {
        let m = {};
        try {
          m = JSON.parse(r.metadata || "{}");
        } catch {}
        return {
          date: m.date || "",
          currency: (m.currency || "").toUpperCase(),
        };
      });
  } catch {
    _reimbReceipts = [];
  }

  // Currency dropdown = currencies present, most-common first (default).
  const counts = {};
  _reimbReceipts.forEach((r) => {
    if (r.currency) counts[r.currency] = (counts[r.currency] || 0) + 1;
  });
  const byFreq = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const csel = $("reimbCurrency");
  csel.innerHTML = byFreq
    .map((c) => `<option value="${c}">${c} (${counts[c]})</option>`)
    .join("");
  if (!byFreq.length) csel.innerHTML = '<option value="">(none)</option>';

  $("reimbFrom").value = "";
  $("reimbTo").value = "";
  _refreshReimbCount();
}

function _refreshReimbCount() {
  const cur = $("reimbCurrency").value;
  const from = $("reimbFrom").value;
  const to = $("reimbTo").value;
  const n = _reimbReceipts.filter((r) => {
    if (cur && r.currency !== cur) return false;
    if (from && r.date && r.date < from) return false;
    if (to && r.date && r.date > to) return false;
    return true;
  }).length;
  $("reimbCount").textContent = `${n} in scope`;
  $("reimbGenBtn").disabled = n === 0;
}

function closeReimbModal() {
  $("reimbModal").classList.remove("open");
}

async function _generateReimb() {
  const btn = $("reimbGenBtn");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Building PDF…';
  $("reimbStatus").className = "status-msg";
  $("reimbStatus").textContent = "Rendering receipts…";
  try {
    const params = new URLSearchParams();
    const uid = $("reimbUserSelect").value;
    const cur = $("reimbCurrency").value;
    const from = $("reimbFrom").value;
    const to = $("reimbTo").value;
    if (uid) params.set("user_id", uid);
    if (cur) params.set("currency", cur);
    if (from) params.set("date_from", from);
    if (to) params.set("date_to", to);
    const res = await fetch(`/api/reports/reimbursement?${params.toString()}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `reembolso_despesas.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    $("reimbStatus").className = "status-msg ok";
    $("reimbStatus").textContent = "Report downloaded.";
    loadReportHistory().catch(() => {});
    setTimeout(closeReimbModal, 1200);
  } catch (e) {
    $("reimbStatus").className = "status-msg err";
    $("reimbStatus").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

(function _wireReimbModal() {
  // Reimbursement report lives on the Reports tab now.
  const open = $("reportsReimbBtn");
  if (open) open.addEventListener("click", openReimbModal);
  const gen = $("reimbGenBtn");
  if (gen) gen.addEventListener("click", _generateReimb);
  ["reimbCurrency", "reimbFrom", "reimbTo"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", _refreshReimbCount);
  });
  // No backdrop-click-to-close (avoid discarding a half-filled form).
})();

// ─── Report history (Reports tab) ──────────────────────────────────────────
async function loadReportHistory() {
  let reports = [];
  try {
    ({ reports } = await api("GET", "/api/report-history"));
  } catch {}
  reports = reports || [];
  const badge = $("navReportCount");
  if (badge) badge.textContent = reports.length;
  const host = $("reportHistoryList");
  if (!host) return;
  if (!reports.length) {
    host.innerHTML =
      '<div class="users-empty">No reports yet. Generate a reimbursement report above, or a statement report from the Reconcile tab.</div>';
    return;
  }
  host.innerHTML = reports
    .map((r) => {
      const when = (r.created_at || "").slice(0, 16).replace("T", " ");
      const kindCls = r.kind === "statement" ? "badge-accent" : "badge-green";
      const kindLbl = r.kind === "statement" ? "Statement" : "Reimbursement";
      const meta = [r.period, r.currency, _fmtSize(r.size)]
        .filter(Boolean)
        .join(" · ");
      return `<div class="rh-row">
        <span class="badge ${kindCls}">${kindLbl}</span>
        <div class="rh-main">
          <div class="rh-title">${_esc(r.title || "Report")}</div>
          <div class="rh-meta">${_esc(meta)} · ${when}</div>
        </div>
        <a class="btn btn-outline btn-sm" href="/api/report-history/${r.id}/download">Download</a>
        <button class="btn btn-ghost btn-sm" onclick="deleteReportHistory(${r.id})">Del</button>
      </div>`;
    })
    .join("");
}

function _fmtSize(bytes) {
  if (!bytes) return "";
  const kb = bytes / 1024;
  return kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.round(kb)} KB`;
}

async function deleteReportHistory(id) {
  if (!confirm("Delete this saved report?")) return;
  try {
    await api("DELETE", `/api/report-history/${id}`);
    loadReportHistory();
  } catch (e) {
    alert("Failed to delete: " + e.message);
  }
}

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
  // Read the pending count straight from the side-panel badge so the modal
  // opens with the right "Processing N files…" copy before the POST returns.
  const pendingTxt = ($("watchPendingBadge")?.textContent || "").match(/\d+/);
  const pending = pendingTxt ? parseInt(pendingTxt[0], 10) : null;
  openScanModal(pending);
  status.textContent = "";
  try {
    const res = await api("POST", "/api/watch-folder/scan");
    if (res.scanned === 0) {
      // Empty inbox — collapse the modal to a quick message and bail out.
      $("scanTitle").textContent = "Inbox is empty";
      $("scanModalSub").textContent =
        "Drop receipts in the folder and scan again.";
      $("scanBar").className = "scan-bar";
      $("scanBarFill").className = "scan-bar-fill done";
      $("scanProgressRow").style.display = "none";
      $("scanFootMsg").className = "scan-foot-msg";
      $("scanFootMsg").textContent = "";
      $("scanCloseBtn").disabled = false;
    } else {
      renderScanResults(res);
    }
    loadReceipts();
    loadStats();
    loadWatchFolder();
    loadReviewQueue();
  } catch (e) {
    $("scanTitle").textContent = "Scan failed";
    $("scanModalSub").textContent = e.message || "Unknown error";
    $("scanBar").className = "scan-bar";
    $("scanBarFill").className = "scan-bar-fill";
    $("scanBarFill").style.background = "var(--red)";
    $("scanBarFill").style.width = "100%";
    $("scanProgressRow").style.display = "none";
    $("scanFootMsg").className = "scan-foot-msg err";
    $("scanFootMsg").textContent = "Error: " + e.message;
    $("scanCloseBtn").disabled = false;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
});

// ─── Folder-scan modal (progress + results) ────────────────────────────────
let _scanAutoCloseTimer = null;
let _scanCountdownTimer = null;
let _scanResultsCache = null; // last results object — needed by detail modal
let _scanDetailFile = null; // filename being edited
let _scanDetailReceipts = []; // working copy in the secondary modal

function openScanModal(pending) {
  _clearScanTimers();
  $("scanModal").classList.add("open");

  $("scanTitle").textContent = "Scanning inbox";
  $("scanModalSub").textContent = "Looking for new receipts…";
  $("scanBar").className = "scan-bar indeterminate";
  $("scanBarFill").className = "scan-bar-fill";
  $("scanBarFill").style.width = "0%";
  $("scanBarFill").style.background = "";

  $("scanProgressRow").style.display = "flex";
  $("scanPending").textContent = pending ?? "…";

  $("scanSummary").style.display = "none";
  $("scanSummary").innerHTML = "";
  $("scanResults").style.display = "none";
  $("scanResults").innerHTML = "";

  $("scanFootMsg").className = "scan-foot-msg";
  $("scanFootMsg").textContent = "This won't take long.";
  $("scanCloseBtn").disabled = true;
}

function renderScanResults(res) {
  _clearScanTimers();
  _scanResultsCache = res;
  const failed = res.files_failed || 0;
  const ok = res.files_ok || 0;
  const imported = res.imported_receipts || 0;
  const scanned = res.scanned || ok + failed;
  const queued = res.queued || 0;
  const allOk = failed === 0;

  $("scanTitle").textContent = allOk
    ? "Scan complete"
    : `Scan finished — ${failed} failed`;
  $("scanModalSub").textContent =
    `${scanned} file${scanned === 1 ? "" : "s"} processed`;
  $("scanBar").className = "scan-bar";
  $("scanBarFill").className = "scan-bar-fill done";
  $("scanBarFill").style.background = allOk ? "" : "var(--amber)";

  $("scanProgressRow").style.display = "none";

  const summary = $("scanSummary");
  summary.style.display = "flex";
  const queuedBadge = queued
    ? `<span class="badge badge-amber">${queued} to review</span>`
    : "";
  if (allOk) {
    summary.innerHTML = `
      <span class="scan-summary-main">${imported} receipt${imported === 1 ? "" : "s"}</span>
      <span style="color:var(--text-3)">from ${scanned} file${scanned === 1 ? "" : "s"}</span>
      <span style="flex:1"></span>
      ${queuedBadge}
      <span class="badge badge-green">${ok} ok</span>`;
  } else {
    summary.innerHTML = `
      <span class="scan-summary-main">${ok} ok</span>
      <span style="color:var(--text-3)">·</span>
      <span style="color:var(--red);font-weight:600">${failed} failed</span>
      <span style="flex:1"></span>
      <span class="badge badge-amber">review</span>`;
  }

  const list = $("scanResults");
  list.style.display = "block";
  list.innerHTML = (res.results || [])
    .map((r, idx) => {
      const okRow = r.status === "ok";
      const name = _escape(r.filename || "(unnamed)");
      const countPill =
        r.count && r.count > 1
          ? `<span class="scan-count-pill">×${r.count}</span>`
          : "";

      let meta = "";
      if (okRow) {
        if (r.queued && !r.count) {
          meta = `${r.queued} page${r.queued === 1 ? "" : "s"} to review`;
        } else if (r.vendor && r.amount != null) {
          meta = `<span class="scan-vendor">${_escape(r.vendor)}</span>${fmt.currency(r.amount, r.currency)}`;
        } else if (r.count && r.count > 1) {
          meta = `${r.count} receipts`;
        } else meta = "saved";
      } else {
        meta = _escape(r.error || "failed");
      }

      const clickable = okRow ? "clickable" : "";
      const editAffordance = okRow
        ? `<span class="scan-row-edit" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg></span>`
        : "";
      const onClick = okRow ? `onclick="openScanDetail(${idx})"` : "";
      const title = okRow ? "Click to edit receipts in this file" : name;

      return `
      <div class="scan-row ${okRow ? "" : "err"} ${clickable}" ${onClick} title="${title}">
        <span class="wl-dot ${okRow ? "ok" : "err"}"></span>
        <span class="scan-row-name">${name}${countPill}</span>
        <span class="scan-row-meta">${meta}</span>
        ${editAffordance}
      </div>`;
    })
    .join("");

  $("scanCloseBtn").disabled = false;
  if (queued) {
    // Multi-receipt pages need box review before OCR — don't auto-close;
    // offer to jump straight into the review flow.
    $("scanFootMsg").className = "scan-foot-msg ok";
    $("scanFootMsg").innerHTML =
      `<button class="btn btn-primary btn-sm" id="scanReviewBtn">Review ${queued} page${queued === 1 ? "" : "s"}</button>`;
    const rb = $("scanReviewBtn");
    if (rb)
      rb.addEventListener("click", () => {
        closeScanModal();
        openReviewQueue();
      });
  } else if (allOk) {
    let n = 4;
    $("scanFootMsg").className = "scan-foot-msg ok";
    $("scanFootMsg").innerHTML =
      `Closing in <span class="scan-countdown">${n}</span>…`;
    _scanCountdownTimer = setInterval(() => {
      n -= 1;
      const cd = $("scanFootMsg").querySelector(".scan-countdown");
      if (cd) cd.textContent = n;
      if (n <= 0) _clearScanTimers();
    }, 1000);
    _scanAutoCloseTimer = setTimeout(closeScanModal, 4000);
  } else {
    $("scanFootMsg").className = "scan-foot-msg err";
    $("scanFootMsg").textContent = "Some files failed — close when ready.";
  }
}

function closeScanModal() {
  _clearScanTimers();
  $("scanModal").classList.remove("open");
}

function _clearScanTimers() {
  if (_scanAutoCloseTimer) {
    clearTimeout(_scanAutoCloseTimer);
    _scanAutoCloseTimer = null;
  }
  if (_scanCountdownTimer) {
    clearInterval(_scanCountdownTimer);
    _scanCountdownTimer = null;
  }
}

function _escape(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

// ─── Secondary modal: edit receipts inside a scanned file ──────────────────
function openScanDetail(idx) {
  if (!_scanResultsCache) return;
  const fileResult = _scanResultsCache.results[idx];
  if (!fileResult || fileResult.status !== "ok") return;

  // Pause auto-close on primary while editing.
  _clearScanTimers();
  $("scanFootMsg").className = "scan-foot-msg";
  $("scanFootMsg").textContent = "Editing — close when ready.";

  _scanDetailFile = fileResult.filename;

  // Build working copy from existing receipts; synthesise empty rows if the
  // backend didn't return per-receipt detail.
  if (Array.isArray(fileResult.receipts) && fileResult.receipts.length) {
    _scanDetailReceipts = fileResult.receipts.map((r) => ({
      vendor: r.vendor || (r.metadata && r.metadata.vendor_name) || "",
      amount: r.amount ?? (r.metadata && r.metadata.total_amount) ?? "",
      currency:
        r.currency ||
        (r.metadata && r.metadata.currency) ||
        fileResult.currency ||
        "USD",
      page: r.page || 1,
    }));
  } else {
    const n = Math.max(1, fileResult.count || 1);
    _scanDetailReceipts = [];
    for (let i = 0; i < n; i++) {
      _scanDetailReceipts.push({
        vendor: i === 0 ? fileResult.vendor || "" : "",
        amount: i === 0 ? (fileResult.amount ?? "") : "",
        currency: fileResult.currency || "USD",
        page: 1,
      });
    }
  }

  $("scanDetailFile").textContent = _scanDetailFile;
  $("scanDetailFile").title = _scanDetailFile;
  renderScanDetailList();
  $("scanDetailModal").classList.add("open");
}

function renderScanDetailList() {
  const list = $("scanDetailList");
  list.innerHTML = _scanDetailReceipts
    .map((r, i) => {
      const amountDisplay =
        r.amount === "" || r.amount == null
          ? ""
          : typeof r.amount === "number"
            ? r.amount.toFixed(2)
            : r.amount;
      return `
      <div class="scan-detail-card">
        <div class="scan-detail-card-h">
          <label class="scan-detail-page" title="Page number">
            Page <input type="number" min="1" value="${r.page}" data-idx="${i}" data-field="page" />
          </label>
          <span class="scan-detail-num">Receipt ${i + 1}</span>
          <button class="scan-detail-remove" title="Remove this receipt" data-remove="${i}"
                  ${_scanDetailReceipts.length === 1 ? 'disabled style="opacity:0.3;cursor:not-allowed"' : ""}>
            ×
          </button>
        </div>
        <div class="scan-detail-fields">
          <input class="scan-detail-input" placeholder="Vendor" value="${_escape(r.vendor || "")}"
                 data-idx="${i}" data-field="vendor" />
          <input class="scan-detail-input amount" placeholder="0.00" inputmode="decimal"
                 value="${amountDisplay}"
                 data-idx="${i}" data-field="amount" />
        </div>
      </div>
    `;
    })
    .join("");

  list.querySelectorAll("input[data-field]").forEach((el) => {
    el.addEventListener("input", (e) => {
      const i = +e.target.dataset.idx;
      const f = e.target.dataset.field;
      _scanDetailReceipts[i][f] =
        f === "page" ? parseInt(e.target.value, 10) || 1 : e.target.value;
    });
  });
  list.querySelectorAll("[data-remove]").forEach((el) => {
    el.addEventListener("click", (e) => {
      const i = +e.currentTarget.dataset.remove;
      if (_scanDetailReceipts.length > 1) {
        _scanDetailReceipts.splice(i, 1);
        renderScanDetailList();
        _refreshDetailTotal();
      }
    });
  });

  _refreshDetailTotal();
}

function _refreshDetailTotal() {
  const n = _scanDetailReceipts.length;
  $("scanDetailCount").textContent = n;
  $("scanDetailCountS").textContent = n === 1 ? "" : "s";
}

function addReceiptRow() {
  const last = _scanDetailReceipts[_scanDetailReceipts.length - 1] || {};
  _scanDetailReceipts.push({
    vendor: "",
    amount: "",
    currency: last.currency || "USD",
    page: last.page || 1,
  });
  renderScanDetailList();
}

function saveScanDetail() {
  if (!_scanResultsCache || !_scanDetailFile) {
    closeScanDetail();
    return;
  }
  const fileResult = _scanResultsCache.results.find(
    (r) => r.filename === _scanDetailFile,
  );
  if (!fileResult) {
    closeScanDetail();
    return;
  }

  const cleaned = _scanDetailReceipts.map((r) => ({
    vendor: (r.vendor || "").trim() || null,
    amount: r.amount === "" || r.amount == null ? null : parseFloat(r.amount),
    currency: r.currency || null,
    page: r.page || 1,
  }));

  fileResult.receipts = cleaned;
  fileResult.count = cleaned.length;
  if (cleaned.length === 1) {
    fileResult.vendor = cleaned[0].vendor;
    fileResult.amount = cleaned[0].amount;
    fileResult.currency = cleaned[0].currency;
  } else {
    fileResult.vendor = null;
    fileResult.amount = null;
  }

  _scanResultsCache.imported_receipts = _scanResultsCache.results.reduce(
    (sum, r) => (r.status === "ok" ? sum + (r.count || 1) : sum),
    0,
  );

  closeScanDetail();
  renderScanResults(_scanResultsCache);
}

function closeScanDetail() {
  $("scanDetailModal").classList.remove("open");
  _scanDetailFile = null;
  _scanDetailReceipts = [];
}

$("scanDetailModal").addEventListener("click", (e) => {
  if (e.target === $("scanDetailModal")) closeScanDetail();
});

$("scanModal").addEventListener("click", (e) => {
  if (e.target === $("scanModal") && !$("scanCloseBtn").disabled)
    closeScanModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("scanDetailModal").classList.contains("open")) {
      closeScanDetail();
    } else if (
      $("scanModal").classList.contains("open") &&
      !$("scanCloseBtn").disabled
    ) {
      closeScanModal();
    }
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

const _BBOX_CHECK = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;

function openBBoxEditor(state) {
  _bboxState = state;
  const isReview = state.reviewItemId != null;
  $("bboxStatus").textContent = "";
  $("bboxStatus").className = "status-msg";
  $("bboxSub").textContent =
    state.reviewSub ||
    "Drag boxes to adjust, drag corners to resize, × to delete, Add to draw a new one.";
  $("bboxDiscardBtn").style.display = isReview ? "" : "none";
  $("bboxCommitBtn").disabled = false;
  $("bboxCommitBtn").innerHTML =
    `${_BBOX_CHECK} ${isReview ? "OCR &amp; next" : "Run OCR"}`;
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
  const isReview = _bboxState.reviewItemId != null;
  const btn = $("bboxCommitBtn");
  const status = $("bboxStatus");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running OCR…';
  status.textContent = `Running OCR on ${boxes.length} crop${boxes.length === 1 ? "" : "s"}…`;
  status.className = "status-msg load";
  try {
    const res = isReview
      ? await api(
          "POST",
          `/api/review-queue/${_bboxState.reviewItemId}/commit`,
          { bboxes: boxes },
        )
      : await api("POST", "/api/upload/receipt/commit", {
          filename: _bboxState.filename,
          description: _bboxState.description || null,
          bboxes: boxes,
        });
    status.textContent = `Saved ${res.count} receipt${res.count === 1 ? "" : "s"}`;
    status.className = "status-msg ok";
    $("receiptStatus").textContent =
      `Saved ${res.count} receipt${res.count === 1 ? "" : "s"}`;
    $("receiptStatus").className = "status-msg ok";
    loadReceipts();
    loadStats();
    if (isReview) {
      _reviewAdvance();
    } else {
      setTimeout(closeBBoxEditor, 600);
    }
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "status-msg err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${_BBOX_CHECK} ${isReview ? "OCR &amp; next" : "Run OCR"}`;
  }
});

// ── Box-review queue: multi-receipt pages from PDF upload / folder scan ──
let _reviewQueue = [];
let _reviewIdx = 0;

async function loadReviewQueue() {
  const card = $("reviewQueueCard");
  if (!card) return;
  try {
    const { items, count } = await api("GET", "/api/review-queue");
    _reviewQueue = items || [];
    card.style.display = (count || 0) > 0 ? "" : "none";
    $("reviewQueueCount").textContent = count || 0;
  } catch {
    card.style.display = "none";
  }
}

async function openReviewQueue() {
  await loadReviewQueue();
  if (!_reviewQueue.length) return;
  _reviewIdx = 0;
  _reviewLoadCurrent();
}

function _reviewLoadCurrent() {
  if (_reviewIdx >= _reviewQueue.length) {
    closeBBoxEditor();
    loadReviewQueue();
    return;
  }
  const it = _reviewQueue[_reviewIdx];
  openBBoxEditor({
    filename: it.filename || "",
    image_url: it.image_url,
    image_size: it.image_size,
    initialBoxes: it.bboxes || [],
    description: it.description || null,
    reviewItemId: it.id,
    reviewSub: `Page ${_reviewIdx + 1} of ${_reviewQueue.length}${it.source ? " · " + it.source : ""} — confirm the receipt boxes`,
  });
}

function _reviewAdvance() {
  _reviewIdx += 1;
  if (_reviewIdx < _reviewQueue.length) {
    _reviewLoadCurrent();
  } else {
    closeBBoxEditor();
    loadReviewQueue();
    loadReceipts();
    loadStats();
  }
}

$("bboxDiscardBtn").addEventListener("click", async () => {
  if (!_bboxState || _bboxState.reviewItemId == null) return;
  try {
    await api("DELETE", `/api/review-queue/${_bboxState.reviewItemId}`);
  } catch (e) {
    $("bboxStatus").textContent = "Discard failed: " + e.message;
    $("bboxStatus").className = "status-msg err";
    return;
  }
  _reviewAdvance();
});

const _reviewBtn = $("reviewQueueBtn");
if (_reviewBtn) _reviewBtn.addEventListener("click", openReviewQueue);

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
  $("editCostCenters").value = meta.cost_centers || "";
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
// No backdrop-click-to-close (avoid discarding a half-filled form).

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
    cost_centers: orNull(trim($("editCostCenters").value)),
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
  users: "Users",
  reports: "Reports",
};
const tabPanels = {
  receipts: $("tab-receipts"),
  reconcile: $("tab-reconcile"),
  statements: $("tab-statements"),
  users: $("tab-users"),
  reports: $("tab-reports"),
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
    // The upload/ingest side panel is only relevant on Receipts.
    const sp = $("sidePanelEl");
    if (sp) sp.classList.toggle("tab-hidden", name !== "receipts");
    $("topbarTitle").textContent = tabTitles[name];
    if (name === "receipts") loadReceipts();
    if (name === "statements") loadStatements();
    if (name === "reconcile") Reconcile.init();
    if (name === "users") loadUsers();
    if (name === "reports") loadReportHistory();
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

    // ── PDF path ─────────────────────────────────────────────────────
    // The /preview endpoint rasterised every page and queued each one for
    // box review (no OCR yet). The user confirms the receipt boxes per page
    // in the bbox editor before OCR runs. Single upload → jump straight into
    // the review flow; bulk → just refresh the queue card and move on.
    if (preview.review_queued) {
      const pages = preview.pages || preview.review_queued;
      setAiStep(
        0,
        "done",
        `PDF received · ${pages} page${pages === 1 ? "" : "s"}`,
      );
      setAiStep(
        1,
        "done",
        `Queued ${preview.review_queued} page${preview.review_queued === 1 ? "" : "s"} for review`,
      );
      setAiStep(2, "active", "Confirm receipt boxes before OCR…");
      const summary = `${preview.review_queued} page${preview.review_queued === 1 ? "" : "s"} queued for box review`;
      if (!bulk) {
        $("receiptStatus").textContent = summary;
        $("receiptStatus").className = "status-msg ok";
        closeAiModal();
        await openReviewQueue();
      } else {
        loadReviewQueue();
      }
      return { ok: true, deferred: true, queued: preview.review_queued };
    }

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

// ─── OCR / Vision model picker (sidebar) ───────────────────────────────────
let _ocrCfg = null;

async function loadOcrConfig() {
  const card = $("ocrModelCard");
  if (!card) return;
  try {
    _ocrCfg = await api("GET", "/api/ocr/config");
    _renderOcrPicker(_ocrCfg);
  } catch (e) {
    $("ocrModelStatus").innerHTML =
      '<span class="ocr-warn">Couldn\'t load OCR config</span>';
  }
}

function _renderOcrPicker(cfg) {
  const sel = $("ocrModelSelect");
  if (!sel) return;
  const models = cfg.models || [];
  const vision = models.filter((m) => m.vision && !m.over_cap);
  const opts = [
    `<option value="apple"${cfg.backend === "apple_vision" ? " selected" : ""}>Apple Vision (on-device)</option>`,
  ];
  if (vision.length) {
    opts.push('<optgroup label="Ollama vision (one-shot)">');
    vision.forEach((m) => {
      const seld =
        cfg.backend === "ollama_vision" && cfg.vision_model === m.name
          ? " selected"
          : "";
      opts.push(
        `<option value="ollama:${_esc(m.name)}"${seld}>${_esc(m.name)} · ${m.size_gb} GB</option>`,
      );
    });
    opts.push("</optgroup>");
  }
  sel.innerHTML = opts.join("");

  // Structuring-model dropdown (only relevant for Apple Vision). Prefer
  // non-vision (text) models, else show all installed.
  const tsel = $("ocrTextModelSelect");
  const textModels = models.filter((m) => !m.vision);
  const list = textModels.length ? textModels : models;
  tsel.innerHTML = list
    .map(
      (m) =>
        `<option value="${_esc(m.name)}"${m.name === cfg.text_model ? " selected" : ""}>${_esc(m.name)} · ${m.size_gb} GB</option>`,
    )
    .join("");
  if (![...tsel.options].some((o) => o.value === cfg.text_model)) {
    tsel.insertAdjacentHTML(
      "afterbegin",
      `<option value="${_esc(cfg.text_model)}" selected>${_esc(cfg.text_model)} (not installed)</option>`,
    );
  }
  $("ocrTextModelRow").style.display = sel.value === "apple" ? "" : "none";
  _renderOcrStatus(cfg);
}

function _renderOcrStatus(cfg) {
  const el = $("ocrModelStatus");
  if (!el) return;
  if (!cfg.ollama_reachable) {
    el.innerHTML =
      '<span class="ocr-warn">Ollama not reachable — start it on :11434</span>';
    return;
  }
  const hasVision = (cfg.models || []).some((m) => m.vision && !m.over_cap);
  if (cfg.backend === "ollama_vision" && !hasVision) {
    el.innerHTML =
      '<span class="ocr-warn">No vision model installed — run <code>ollama pull glm-ocr</code></span>';
  } else if (
    cfg.backend === "apple_vision" &&
    !(cfg.models || []).some((m) => m.name === cfg.text_model)
  ) {
    el.innerHTML = `<span class="ocr-warn">Structuring model <code>${_esc(cfg.text_model)}</code> not installed</span>`;
  } else {
    el.innerHTML = '<span class="ocr-ok">Applies on next OCR.</span>';
  }
}

async function _saveOcrConfig() {
  const sel = $("ocrModelSelect");
  let patch;
  if (sel.value === "apple") {
    patch = {
      backend: "apple_vision",
      text_model: $("ocrTextModelSelect").value,
    };
  } else {
    patch = {
      backend: "ollama_vision",
      vision_model: sel.value.replace(/^ollama:/, ""),
    };
  }
  try {
    _ocrCfg = await api("PUT", "/api/ocr/config", patch);
    // Re-fetch the model list so status reflects reality.
    await loadOcrConfig();
  } catch (e) {
    $("ocrModelStatus").innerHTML =
      '<span class="ocr-warn">Failed: ' + _esc(e.message) + "</span>";
  }
}

(function _wireOcrPicker() {
  const sel = $("ocrModelSelect");
  if (sel)
    sel.addEventListener("change", () => {
      $("ocrTextModelRow").style.display = sel.value === "apple" ? "" : "none";
      _saveOcrConfig();
    });
  const tsel = $("ocrTextModelSelect");
  if (tsel) tsel.addEventListener("change", _saveOcrConfig);
})();

// ─── Init ─────────────────────────────────────────────────────────────────────
loadStats().catch(() => {});
loadReceipts().catch(() => {});
loadStatements().catch(() => {});
loadFxRates().catch(() => {});
loadWatchFolder().catch(() => {});
loadGmailStatus().catch(() => {});
loadUsers().catch(() => {});
loadReportHistory().catch(() => {});
restoreLastSync();
checkOllama().catch(() => {});
loadOcrConfig().catch(() => {});
loadReviewQueue().catch(() => {});

// ─── Filter bar + sortable column wiring (runs once at load) ────────────────
(function _wireReceiptFiltersAndSort() {
  // Restore filter UI from persisted state.
  const fields = [
    ["rfSearch", "q", "input"],
    ["rfFromDate", "from", "change"],
    ["rfToDate", "to", "change"],
    ["rfMinAmt", "min", "input"],
    ["rfMaxAmt", "max", "input"],
    ["rfStatus", "status", "change"],
    ["rfCategory", "category", "change"],
    ["rfCurrency", "currency", "change"],
  ];
  fields.forEach(([id, key, evt]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = receiptFilters[key] || "";
    el.addEventListener(evt, () => {
      receiptFilters[key] = el.value;
      _saveReceiptFilters();
      _updateFilterBadge();
      renderReceipts(allReceipts);
      updateProgress(allReceipts);
    });
  });

  const clearBtn = document.getElementById("rfClearBtn");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      receiptFilters = { ..._FILTER_DEFAULTS };
      fields.forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el) el.value = _FILTER_DEFAULTS[key] || "";
      });
      _saveReceiptFilters();
      _updateFilterBadge();
      renderReceipts(allReceipts);
      updateProgress(allReceipts);
    });
  }
  _updateFilterBadge();

  // Sortable column headers — click a <th data-sort="col">.
  // Same handler shape works for any sortable table; receipts is the only
  // one wired to live data right now, matches gets wired when rendered.
  document
    .querySelectorAll('table.sortable[data-table="receipts"] th[data-sort]')
    .forEach((th) => {
      th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (receiptSort.col === col) {
          receiptSort.dir = receiptSort.dir === "asc" ? "desc" : "asc";
        } else {
          // Default to descending for date/amount (most-recent / largest
          // first feels natural); ascending for everything text-y.
          const numeric = col === "date" || col === "amount";
          receiptSort = { col, dir: numeric ? "desc" : "asc" };
        }
        _saveSort("receipts", receiptSort);
        _refreshSortIndicators("receipts", receiptSort);
        renderReceipts(allReceipts);
      });
    });
  _refreshSortIndicators("receipts", receiptSort);
})();
