/**
 * escalations.js
 * ==============
 * Wires the Escalations Queue page to GET /exceptions.
 * Depends on api-client.js (loaded first via <script> tag).
 *
 * Features:
 *  - Fetch all exceptions on load
 *  - Group by reason → normalize to section headers
 *  - Render grouped accordion sections dynamically
 *  - "View Trace" buttons link to /ui/trace.html?txn={id}
 *  - Client-side search input filters by transaction ID substring
 *  - Empty state when no exceptions
 *  - Live "N Open" count badge in header
 */

// ---------------------------------------------------------------------------
// Reason → section header mapping
// ---------------------------------------------------------------------------

const SECTION_DEFS = [
  {
    key: 'risk_hold',
    label: 'Risk Hold',
    icon: 'warning',
    iconColor: 'text-tertiary-container',
    badgeClass: 'bg-tertiary-container/10 text-tertiary-container',
    match: (r) => /risk.?hold|risk_hold/i.test(r),
  },
  {
    key: 'max_retries',
    label: 'Max Retries Exceeded',
    icon: 'sync_problem',
    iconColor: 'text-surface-tint',
    badgeClass: 'bg-surface-container text-on-surface-variant',
    match: (r) => /max.?retr|retry.?exceeded|retry_exceeded|max_retry/i.test(r),
  },
  {
    key: 'diagnosis_failure',
    label: 'Diagnosis Failure',
    icon: 'bug_report',
    iconColor: 'text-surface-tint',
    badgeClass: 'bg-surface-container text-on-surface-variant',
    match: (r) => /diagnosis.?fail|diagnos.*error|classify/i.test(r),
  },
];

const FALLBACK_SECTION = {
  key: 'other',
  label: 'Other Escalations',
  icon: 'assignment_late',
  iconColor: 'text-outline',
  badgeClass: 'bg-surface-container text-on-surface-variant',
};

function categorize(reason) {
  for (const def of SECTION_DEFS) {
    if (def.match(reason)) return def.key;
  }
  return 'fallback';
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtDatetime(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  return d.toLocaleString('en-IN', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function fmtRupee(val) {
  if (val === null || val === undefined) return '—';
  return `₹ ${Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const TIER_STYLES = {
  enterprise: 'bg-secondary-container/20 text-secondary',
  business:   'bg-surface-container-highest text-on-surface',
  standard:   'bg-surface-container-highest text-on-surface',
};
function tierBadge(tier) {
  const cls = TIER_STYLES[(tier ?? '').toLowerCase()] ?? 'bg-surface-container-highest text-on-surface';
  return `<span class="inline-flex items-center px-2 py-1 rounded ${cls} font-label-uppercase text-[10px]">${escHtml(tier ?? '—')}</span>`;
}

// ---------------------------------------------------------------------------
// Table row builder
// ---------------------------------------------------------------------------

function buildRow(exc, visible = true) {
  return `
    <tr class="hover:bg-slate-100/50 transition-colors group/row exc-row"
        data-txn-id="${escHtml(exc.transaction_id)}"
        ${visible ? '' : 'style="display:none"'}>
      <td class="px-4 py-3 font-numeric-data text-numeric-data text-on-surface">${escHtml(exc.transaction_id)}</td>
      <td class="px-4 py-3">${tierBadge(exc.customer_value_tier)}</td>
      <td class="px-4 py-3 font-numeric-data text-numeric-data text-on-surface font-medium">${fmtRupee(exc.amount)}</td>
      <td class="px-4 py-3 font-numeric-data text-numeric-data text-on-surface-variant">${fmtDatetime(exc.escalated_at)}</td>
      <td class="px-4 py-3 text-right">
        <a href="/ui/trace.html?txn=${encodeURIComponent(exc.transaction_id)}"
           class="text-secondary font-body-sm text-body-sm font-medium hover:underline opacity-0 group-hover/row:opacity-100 transition-opacity">
          View Trace
        </a>
      </td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Section builder
// ---------------------------------------------------------------------------

function buildSection(def, items) {
  const tableRows = items.map(e => buildRow(e)).join('');
  return `
    <div class="bg-surface rounded-xl border border-outline-variant overflow-hidden group exc-section" data-section="${def.key}">
      <button class="w-full flex items-center justify-between p-4 bg-surface hover:bg-surface-container-lowest transition-colors text-left border-b border-outline-variant/50">
        <div class="flex items-center gap-3">
          <span class="material-symbols-outlined ${def.iconColor} text-[20px]">${def.icon}</span>
          <h2 class="font-headline-sm text-headline-sm text-on-surface">${escHtml(def.label)}</h2>
          <span class="${def.badgeClass} px-2 py-0.5 rounded-full font-label-uppercase text-label-uppercase ml-2 section-count">${items.length} Item${items.length !== 1 ? 's' : ''}</span>
        </div>
        <span class="material-symbols-outlined text-outline transition-transform duration-200 group-hover:text-on-surface">expand_more</span>
      </button>
      <div class="overflow-x-auto section-body">
        <table class="w-full text-left border-collapse min-w-[800px]">
          <thead class="bg-surface-container-lowest border-b border-outline-variant/30">
            <tr>
              <th class="px-4 py-3 font-label-uppercase text-label-uppercase text-on-surface-variant w-1/5">Transaction ID</th>
              <th class="px-4 py-3 font-label-uppercase text-label-uppercase text-on-surface-variant w-1/5">Customer Tier</th>
              <th class="px-4 py-3 font-label-uppercase text-label-uppercase text-on-surface-variant w-1/5">Amount</th>
              <th class="px-4 py-3 font-label-uppercase text-label-uppercase text-on-surface-variant w-1/5">Escalated At</th>
              <th class="px-4 py-3 font-label-uppercase text-label-uppercase text-on-surface-variant w-1/5 text-right">Action</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-outline-variant/20">
            ${tableRows || `<tr><td colspan="5" class="px-4 py-6 text-center text-on-surface-variant font-body-sm text-body-sm">No items in this category.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Render all sections
// ---------------------------------------------------------------------------

function renderSections(exceptions) {
  const container = document.getElementById('escalations-container');
  if (!container) return;

  if (exceptions.length === 0) {
    container.innerHTML = '';
    setVisible('escalations-empty-state', true);
    setVisible('escalations-container',   false);
    updateOpenCount(0);
    return;
  }

  setVisible('escalations-empty-state', false);
  setVisible('escalations-container',   true);
  updateOpenCount(exceptions.length);

  // Group exceptions
  const groups = {};
  for (const exc of exceptions) {
    const key = categorize(exc.reason);
    if (!groups[key]) groups[key] = [];
    groups[key].push(exc);
  }

  // Build sections: fixed order from SECTION_DEFS, then fallback
  const parts = [];
  for (const def of SECTION_DEFS) {
    if (groups[def.key]?.length) {
      parts.push(buildSection(def, groups[def.key]));
    }
  }
  if (groups['fallback']?.length) {
    parts.push(buildSection(FALLBACK_SECTION, groups['fallback']));
  }

  container.innerHTML = parts.join('');
}

// ---------------------------------------------------------------------------
// Client-side search filter
// ---------------------------------------------------------------------------

function applySearch(query) {
  const q = query.trim().toLowerCase();
  const rows = document.querySelectorAll('.exc-row');

  rows.forEach(row => {
    const txnId = (row.dataset.txnId || '').toLowerCase();
    row.style.display = (!q || txnId.includes(q)) ? '' : 'none';
  });

  // Update per-section counts based on visible rows
  document.querySelectorAll('.exc-section').forEach(section => {
    const visible = section.querySelectorAll('.exc-row:not([style*="display: none"]):not([style*="display:none"])');
    const countEl = section.querySelector('.section-count');
    if (countEl) {
      countEl.textContent = `${visible.length} Item${visible.length !== 1 ? 's' : ''}`;
    }
    // Hide entire section if no visible rows
    if (q) {
      section.style.display = visible.length === 0 ? 'none' : '';
    } else {
      section.style.display = '';
    }
  });
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function setVisible(id, visible) {
  const el = document.getElementById(id);
  if (!el) return;
  if (visible) el.classList.remove('hidden');
  else          el.classList.add('hidden');
}

function updateOpenCount(n) {
  const el = document.getElementById('escalations-open-count');
  if (el) el.textContent = `${n} Open`;
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  // Wire search input
  const searchInput = document.getElementById('escalations-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => applySearch(searchInput.value));
  }

  // Show loading state
  const container = document.getElementById('escalations-container');
  if (container) {
    container.innerHTML = `
      <div class="flex items-center justify-center py-12">
        <span class="animate-spin material-symbols-outlined text-outline text-[32px] mr-3">refresh</span>
        <span class="font-body-md text-body-md text-on-surface-variant">Loading escalations…</span>
      </div>`;
  }

  try {
    const exceptions = await fetchExceptions();
    renderSections(exceptions);
  } catch (err) {
    console.error('fetchExceptions failed:', err);
    if (container) {
      container.innerHTML = `
        <div class="bg-error-container/30 rounded-xl border border-error/20 p-8 text-center">
          <span class="material-symbols-outlined text-error text-[32px] mb-3 block">error</span>
          <p class="font-body-md text-body-md text-on-surface">Failed to load escalations: ${escHtml(err.message)}</p>
        </div>`;
    }
  }
});
