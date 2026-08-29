/**
 * overview.js
 * ===========
 * Wires the Overview dashboard to the live FastAPI backend.
 * Depends on api-client.js (loaded first via <script> tag).
 *
 * Responsibilities:
 *  - Populate 4 KPI cards from GET /metrics
 *  - Populate "Records Processed" + "Escalated Count" from same metrics response
 *  - Populate Status Breakdown bar from transactions data
 *  - Populate Recent Transactions table from GET /transactions
 *  - Wire Status / Tier / Failure Code filter selects to re-fetch on change
 *  - Wire "Run Recovery Cycle" button with loading state + error toast
 */

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** Format a 0-1 float as a percentage string, e.g. 0.842 → "84.2%" */
function fmtPct(val) {
  if (val === null || val === undefined) return '—';
  return `${(val * 100).toFixed(1)}%`;
}

/** Format a rupee amount with compact suffix, e.g. 4200000 → "₹4.2M" */
function fmtRupee(val) {
  if (val === null || val === undefined) return '—';
  if (Math.abs(val) >= 1_000_000) return `₹${(val / 1_000_000).toFixed(1)}M`;
  if (Math.abs(val) >= 1_000)    return `₹${(val / 1_000).toFixed(1)}K`;
  return `₹${val.toFixed(0)}`;
}

/** Format seconds to human-readable duration, e.g. 15120 → "4.2 hrs" */
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60)           return `${Math.round(seconds)}s`;
  if (seconds < 3600)         return `${(seconds / 60).toFixed(1)} min`;
  if (seconds < 86400)        return `${(seconds / 3600).toFixed(1)} hrs`;
  return `${(seconds / 86400).toFixed(1)} days`;
}

/** Format an ISO datetime to readable short form, e.g. "Aug 29, 10:42" */
function fmtDatetime(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  return d.toLocaleString('en-IN', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

/** Format a number with locale commas, e.g. 12402 → "12,402" */
function fmtCount(n) {
  if (n === null || n === undefined) return '—';
  return Number(n).toLocaleString('en-IN');
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------

function showToast(message, type = 'error') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  const bg    = type === 'error'   ? 'bg-red-600'
              : type === 'success' ? 'bg-green-600'
              : 'bg-gray-700';
  toast.className = `${bg} text-white px-4 py-3 rounded-lg shadow-lg flex items-center gap-3
                     font-body-sm text-body-sm max-w-sm transition-all duration-300 opacity-0`;
  toast.innerHTML = `
    <span class="material-symbols-outlined text-[18px] shrink-0">
      ${type === 'error' ? 'error' : type === 'success' ? 'check_circle' : 'info'}
    </span>
    <span class="flex-1">${message}</span>
    <button onclick="this.parentElement.remove()" class="opacity-70 hover:opacity-100 shrink-0">
      <span class="material-symbols-outlined text-[16px]">close</span>
    </button>`;
  container.appendChild(toast);

  // Fade in
  requestAnimationFrame(() => { toast.classList.remove('opacity-0'); toast.classList.add('opacity-100'); });

  // Auto-dismiss after 5 s
  setTimeout(() => {
    toast.classList.add('opacity-0');
    toast.addEventListener('transitionend', () => toast.remove());
  }, 5000);
}

// ---------------------------------------------------------------------------
// Status badge helpers (matches existing Stitch design tokens)
// ---------------------------------------------------------------------------

const STATUS_STYLES = {
  recovered:     'bg-[#15803d]/10 text-[#15803d]',
  escalated:     'bg-[#b91c1c]/10 text-[#b91c1c]',
  pending_retry: 'bg-[#eab308]/10 text-[#b45309]',
  failed:        'bg-red-100 text-red-700',
  abandoned:     'bg-surface-variant text-on-surface-variant',
};
function statusBadge(status) {
  const cls = STATUS_STYLES[status] ?? 'bg-gray-100 text-gray-700';
  return `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ${cls}">
            ${status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
          </span>`;
}

const TIER_STYLES = {
  enterprise: 'bg-[#9333ea]/10 text-[#9333ea]',
  business:   'bg-[#1e40af]/10 text-[#1e40af]',
  standard:   'bg-surface-tint/10 text-surface-tint',
};
function tierBadge(tier) {
  const cls = TIER_STYLES[tier?.toLowerCase()] ?? 'bg-gray-100 text-gray-700';
  return `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ${cls}">
            ${tier ?? '—'}
          </span>`;
}

// ---------------------------------------------------------------------------
// KPI card population
// ---------------------------------------------------------------------------

function populateMetrics(m) {
  setText('kpi-recovery-rate',     fmtPct(m.recovery_rate));
  setText('kpi-total-recovered',   fmtRupee(m.total_recovered));
  setText('kpi-total-at-risk',     fmtRupee(m.total_at_risk));
  setText('kpi-mtr',               m.mean_time_to_recovery_formatted || fmtDuration(m.mean_time_to_recovery));
  setText('stat-records-processed', fmtCount(m.records_processed));
  setText('stat-escalated-count',   fmtCount(m.escalated_count));
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ---------------------------------------------------------------------------
// Status breakdown bar
// ---------------------------------------------------------------------------

function populateStatusBar(transactions) {
  const total = transactions.length;
  if (total === 0) return;

  const counts = { recovered: 0, escalated: 0, pending_retry: 0, abandoned: 0, other: 0 };
  for (const tx of transactions) {
    const s = tx.status;
    if (s in counts) counts[s]++;
    else counts.other++;
  }

  // Bar segments
  setBarWidth('status-bar-recovered',     counts.recovered,     total);
  setBarWidth('status-bar-escalated',     counts.escalated,     total);
  setBarWidth('status-bar-pending-retry', counts.pending_retry, total);
  setBarWidth('status-bar-abandoned',     counts.abandoned + counts.other, total);

  // Legend values
  const fmt = (k) => `${pct(counts[k], total)}%`;
  setText('legend-recovered',     `${fmt('recovered')} (${fmtRupee(sumAmount(transactions, 'recovered'))})`);
  setText('legend-escalated',     `${fmt('escalated')} (${fmtRupee(sumAmount(transactions, 'escalated'))})`);
  setText('legend-pending-retry', `${fmt('pending_retry')} (${fmtRupee(sumAmount(transactions, 'pending_retry'))})`);
  const abandonedAmt = sumAmount(transactions, 'abandoned') + sumAmount(transactions, 'failed');
  setText('legend-abandoned',     `${fmt('abandoned')} (${fmtRupee(abandonedAmt)})`);
}

function setBarWidth(id, count, total) {
  const el = document.getElementById(id);
  if (el) el.style.width = `${pct(count, total)}%`;
}

function pct(count, total) {
  return total === 0 ? 0 : ((count / total) * 100).toFixed(1);
}

function sumAmount(transactions, status) {
  return transactions.filter(t => t.status === status).reduce((s, t) => s + (t.amount || 0), 0);
}

// ---------------------------------------------------------------------------
// Transactions table
// ---------------------------------------------------------------------------

function populateTransactionsTable(transactions) {
  const tbody = document.getElementById('transactions-tbody');
  if (!tbody) return;

  if (transactions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="px-4 py-8 text-center text-on-surface-variant font-body-sm text-body-sm">
      No transactions match the current filters.</td></tr>`;
    return;
  }

  tbody.innerHTML = transactions.map(tx => `
    <tr class="hover:bg-surface-container-low transition-colors group">
      <td class="px-4 py-3 font-numeric-data text-numeric-data">${tx.id}</td>
      <td class="px-4 py-3">${tierBadge(tx.customer_value_tier)}</td>
      <td class="px-4 py-3 font-numeric-data text-numeric-data text-right">₹${Number(tx.amount).toLocaleString('en-IN')}</td>
      <td class="px-4 py-3">${statusBadge(tx.status)}</td>
      <td class="px-4 py-3 font-mono text-[11px] text-on-surface-variant">${tx.failure_code}</td>
      <td class="px-4 py-3 text-on-surface-variant whitespace-nowrap">${fmtDatetime(tx.created_at)}</td>
      <td class="px-4 py-3 text-right">
        <a href="/ui/trace.html?txn=${encodeURIComponent(tx.id)}"
           class="text-secondary opacity-0 group-hover:opacity-100 transition-opacity font-label-uppercase text-label-uppercase hover:underline">
          View
        </a>
      </td>
    </tr>`).join('');
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function showTableSkeleton() {
  const tbody = document.getElementById('transactions-tbody');
  if (!tbody) return;
  tbody.innerHTML = Array(5).fill('').map(() => `
    <tr class="animate-pulse">
      ${Array(7).fill('<td class="px-4 py-3"><div class="h-4 bg-surface-container-high rounded w-3/4"></div></td>').join('')}
    </tr>`).join('');
}

function showKpiSkeleton() {
  ['kpi-recovery-rate','kpi-total-recovered','kpi-total-at-risk','kpi-mtr',
   'stat-records-processed','stat-escalated-count'].forEach(id => setText(id, '…'));
}

// ---------------------------------------------------------------------------
// Current filter state
// ---------------------------------------------------------------------------

let currentFilters = {};

function readFilters() {
  const status = document.getElementById('filter-status')?.value || '';
  const tier   = document.getElementById('filter-tier')?.value   || '';
  const code   = document.getElementById('filter-failure-code')?.value || '';
  return {
    ...(status ? { status }                               : {}),
    ...(tier   ? { customer_value_tier: tier }            : {}),
    ...(code   ? { failure_code: code }                   : {}),
    limit: 50,
  };
}

// ---------------------------------------------------------------------------
// Main refresh functions
// ---------------------------------------------------------------------------

async function refreshMetrics() {
  try {
    const m = await fetchMetrics();
    populateMetrics(m);
  } catch (err) {
    console.error('fetchMetrics failed:', err);
    showToast(`Failed to load metrics: ${err.message}`);
  }
}

async function refreshTransactions() {
  showTableSkeleton();
  try {
    const txns = await fetchTransactions(currentFilters);
    populateTransactionsTable(txns);
    populateStatusBar(txns);
  } catch (err) {
    console.error('fetchTransactions failed:', err);
    const tbody = document.getElementById('transactions-tbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="px-4 py-8 text-center text-red-600 font-body-sm text-body-sm">
      Failed to load transactions: ${err.message}</td></tr>`;
    showToast(`Failed to load transactions: ${err.message}`);
  }
}

async function refreshAll() {
  await Promise.all([refreshMetrics(), refreshTransactions()]);
}

// ---------------------------------------------------------------------------
// Run Recovery Cycle button
// ---------------------------------------------------------------------------

function wireRunButton() {
  const btn = document.getElementById('run-cycle-btn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    // Loading state
    btn.disabled = true;
    const originalHTML = btn.innerHTML;
    btn.innerHTML = `<span class="animate-spin material-symbols-outlined text-[18px]">refresh</span>
                     <span class="hidden sm:inline">Running…</span>`;

    try {
      const result = await runPipelineCycle(100);
      showToast(
        `Cycle complete: ${result.processed} processed, ${result.recovered_count} recovered, ₹${Number(result.total_recovered_amount).toLocaleString('en-IN')} recovered.`,
        'success'
      );
      await refreshAll();
    } catch (err) {
      console.error('runPipelineCycle failed:', err);
      showToast(`Recovery cycle failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalHTML;
    }
  });
}

// ---------------------------------------------------------------------------
// Filter wiring
// ---------------------------------------------------------------------------

function wireFilters() {
  ['filter-status', 'filter-tier', 'filter-failure-code'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('change', () => {
        currentFilters = readFilters();
        refreshTransactions();
      });
    }
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  showKpiSkeleton();
  showTableSkeleton();
  wireRunButton();
  wireFilters();
  currentFilters = readFilters();
  await refreshAll();
});
