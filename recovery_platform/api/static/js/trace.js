/**
 * trace.js
 * ========
 * Wires the Audit Trace Inspector page to GET /transactions/{id}/trace.
 * Depends on api-client.js (loaded first via <script> tag).
 *
 * Features:
 *  - Deep-link support: ?txn=TXN-9021 auto-populates input and runs search
 *  - Search button + Enter key on input trigger fetchTrace()
 *  - Renders full timeline: Transaction Failed → Diagnosis → Recovery Action →
 *    Outcome → Exception Logged (conditional)
 *  - 404 → "Transaction not found" state, not broken page
 *  - Loading spinner while in-flight
 */

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtDatetime(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  return d.toLocaleString('en-IN', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  });
}

function fmtRupee(val) {
  if (val === null || val === undefined) return '—';
  return `₹${Number(val).toLocaleString('en-IN')}`;
}

function fmtPct(val) {
  if (val === null || val === undefined) return '—';
  return `${(val * 100).toFixed(0)}%`;
}

/** Compute stroke-dashoffset so the arc fills to the confidence percentage */
function confidenceDashOffset(confidence) {
  // Circle circumference ≈ 2π×16 ≈ 100.5 — Stitch used 100 for simplicity
  const circumference = 100;
  return circumference - (confidence * circumference);
}

function confidenceLabel(conf) {
  if (conf >= 0.9) return 'Very High Confidence';
  if (conf >= 0.75) return 'High Confidence';
  if (conf >= 0.5) return 'Medium Confidence';
  return 'Low Confidence';
}

// ---------------------------------------------------------------------------
// Timeline section builders
// ---------------------------------------------------------------------------

/**
 * Build the "Transaction Failed" timeline event HTML.
 */
function buildFailedEvent(tx) {
  return `
    <div class="relative">
      <div class="absolute -left-[41px] top-0 w-8 h-8 rounded-full bg-error-container border-2 border-surface-container-lowest flex items-center justify-center text-error z-10">
        <span class="material-symbols-outlined text-[18px]">error_outline</span>
      </div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-start">
          <h4 class="font-numeric-data text-numeric-data text-on-surface font-semibold flex items-center gap-2">
            Transaction Failed
          </h4>
          <span class="font-body-sm text-body-sm text-on-surface-variant">${fmtDatetime(tx.created_at)}</span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-2">
          <div class="bg-surface-container-low p-4 rounded border border-surface-variant">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Amount</span>
            <span class="font-headline-sm text-headline-sm text-on-surface">${fmtRupee(tx.amount)}</span>
          </div>
          <div class="bg-surface-container-low p-4 rounded border border-surface-variant">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Failure Code</span>
            <span class="font-numeric-data text-numeric-data text-error font-medium">${tx.failure_code ?? '—'}</span>
          </div>
          <div class="bg-surface-container-low p-4 rounded border border-surface-variant">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Customer Tier</span>
            <span class="inline-block mt-1 px-2 py-0.5 bg-primary-fixed/20 text-primary border border-primary-fixed rounded text-xs font-semibold">
              ${tx.customer_value_tier ?? '—'}
            </span>
          </div>
        </div>
      </div>
    </div>`;
}

/**
 * Build a "Diagnosis" timeline event HTML.
 */
function buildDiagnosisEvent(d) {
  const dashOffset = confidenceDashOffset(d.confidence).toFixed(1);
  return `
    <div class="relative">
      <div class="absolute -left-[41px] top-0 w-8 h-8 rounded-full bg-primary-fixed border-2 border-surface-container-lowest flex items-center justify-center text-primary z-10">
        <span class="material-symbols-outlined text-[18px]" style="font-variation-settings: 'FILL' 1;">psychology</span>
      </div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-start">
          <div class="flex items-center gap-3">
            <h4 class="font-numeric-data text-numeric-data text-on-surface font-semibold">Diagnosis</h4>
            <span class="px-2 py-0.5 bg-surface-container-high text-on-surface-variant border border-outline-variant rounded-full text-xs font-medium">
              ${d.classification}
            </span>
          </div>
          <span class="font-body-sm text-body-sm text-on-surface-variant">${fmtDatetime(d.created_at)}</span>
        </div>
        <div class="bg-surface-container-low p-4 rounded border border-surface-variant mt-2 flex flex-col md:flex-row gap-6 items-start md:items-center">
          <div class="flex items-center gap-4 shrink-0">
            <div class="relative w-12 h-12 flex items-center justify-center">
              <svg class="w-full h-full -rotate-90" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
                <circle class="stroke-current text-outline-variant" cx="18" cy="18" fill="none" r="16" stroke-width="4"></circle>
                <circle class="stroke-current text-secondary" cx="18" cy="18" fill="none" r="16"
                  stroke-dasharray="100" stroke-dashoffset="${dashOffset}"
                  stroke-linecap="round" stroke-width="4"></circle>
              </svg>
              <span class="absolute font-label-uppercase text-label-uppercase text-on-surface">${fmtPct(d.confidence)}</span>
            </div>
            <div>
              <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant">Confidence</span>
              <span class="font-numeric-data text-numeric-data text-on-surface">${confidenceLabel(d.confidence)}</span>
            </div>
          </div>
          <div class="w-px h-12 bg-surface-variant hidden md:block"></div>
          <div class="flex-1">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Reasoning</span>
            <p class="font-body-sm text-body-sm text-on-surface whitespace-pre-wrap">${escHtml(d.reasoning)}</p>
          </div>
        </div>
      </div>
    </div>`;
}

/**
 * Build a "Recovery Action" timeline event HTML.
 */
function buildActionEvent(a) {
  const boundTags = (a.bounds_applied || []).map(b =>
    `<span class="px-2 py-1 bg-surface-container-high text-on-surface-variant border border-outline-variant rounded text-[11px] font-mono tracking-wider">${escHtml(b)}</span>`
  ).join('');

  return `
    <div class="relative">
      <div class="absolute -left-[41px] top-0 w-8 h-8 rounded-full bg-secondary-fixed border-2 border-surface-container-lowest flex items-center justify-center text-on-secondary-fixed z-10">
        <span class="material-symbols-outlined text-[18px]">autorenew</span>
      </div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-start">
          <div class="flex items-center gap-3">
            <h4 class="font-numeric-data text-numeric-data text-on-surface font-semibold">Recovery Action</h4>
            <span class="px-2 py-0.5 bg-secondary/10 text-secondary border border-secondary/20 rounded-full text-xs font-medium">
              ${escHtml(a.action_type)}
            </span>
          </div>
          <span class="font-body-sm text-body-sm text-on-surface-variant">${fmtDatetime(a.created_at)}</span>
        </div>
        <div class="bg-surface-container-low p-4 rounded border border-surface-variant mt-2">
          <div class="mb-4">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Justification</span>
            <p class="font-body-sm text-body-sm text-on-surface whitespace-pre-wrap">${escHtml(a.justification)}</p>
          </div>
          ${boundTags ? `
          <div>
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-2">Applied Rules / Tags</span>
            <div class="flex flex-wrap gap-2">${boundTags}</div>
          </div>` : ''}
        </div>
      </div>
    </div>`;
}

/**
 * Build an "Outcome" timeline event HTML.
 */
function buildOutcomeEvent(o) {
  const isSuccess = o.final_status === 'recovered';
  const statusColor = isSuccess ? 'text-[#137333]' : 'text-[#b91c1c]';
  const borderColor = isSuccess ? 'border-[#CEEAD6]' : 'border-[#ffdad6]';
  const iconColor   = isSuccess ? 'bg-[#E6F4EA] text-[#137333]' : 'bg-error-container text-error';
  const icon        = isSuccess ? 'check_circle' : 'cancel';
  const label       = isSuccess ? 'Recovered' : o.final_status.replace(/_/g, ' ');

  return `
    <div class="relative">
      <div class="absolute -left-[41px] top-0 w-8 h-8 rounded-full ${iconColor} border-2 border-surface-container-lowest flex items-center justify-center z-10">
        <span class="material-symbols-outlined text-[18px]" style="font-variation-settings: 'FILL' 1;">${icon}</span>
      </div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-start">
          <h4 class="font-numeric-data text-numeric-data text-on-surface font-semibold">Outcome</h4>
          <span class="font-body-sm text-body-sm text-on-surface-variant">${fmtDatetime(o.resolved_at)}</span>
        </div>
        <div class="bg-surface-container-low p-4 rounded border ${borderColor} mt-2 flex flex-col md:flex-row gap-8 items-center">
          <div class="flex flex-col items-center md:items-start text-center md:text-left">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Status</span>
            <span class="font-headline-sm text-headline-sm ${statusColor} font-bold">${label}</span>
          </div>
          <div class="w-full md:w-px h-px md:h-12 bg-surface-variant"></div>
          <div class="flex flex-col items-center md:items-start text-center md:text-left">
            <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Amount Recovered</span>
            <span class="font-headline-sm text-headline-sm text-on-surface font-semibold">${fmtRupee(o.amount_recovered)}</span>
          </div>
        </div>
      </div>
    </div>`;
}

/**
 * Build an "Exception Logged" timeline event HTML.
 */
function buildExceptionEvent(e) {
  return `
    <div class="relative">
      <div class="absolute -left-[41px] top-0 w-8 h-8 rounded-full bg-error-container border-2 border-surface-container-lowest flex items-center justify-center text-error z-10">
        <span class="material-symbols-outlined text-[18px]" style="font-variation-settings: 'FILL' 1;">assignment_late</span>
      </div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-start">
          <h4 class="font-numeric-data text-numeric-data text-on-surface font-semibold text-error">Exception Logged</h4>
          <span class="font-body-sm text-body-sm text-on-surface-variant">${fmtDatetime(e.escalated_at)}</span>
        </div>
        <div class="bg-error-container/30 p-4 rounded border border-error/20 mt-2">
          <span class="block font-label-uppercase text-label-uppercase text-on-surface-variant mb-1">Reason</span>
          <p class="font-body-sm text-body-sm text-on-surface">${escHtml(e.reason)}</p>
        </div>
      </div>
    </div>`;
}

/** Escape HTML special chars */
function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Render timeline from trace response
// ---------------------------------------------------------------------------

function renderTrace(data) {
  const tx  = data.transaction;
  const dia = data.diagnoses || [];
  const act = data.recovery_actions || [];
  const out = data.outcomes || [];
  const exc = data.exception_logs || [];

  const eventCount = 1 + dia.length + act.length + out.length + exc.length;

  // Header
  const header = document.getElementById('trace-header');
  if (header) {
    header.innerHTML = `
      <div>
        <h3 class="font-headline-sm text-headline-sm text-on-surface">Transaction: ${escHtml(tx.id)}</h3>
        <p class="font-body-sm text-body-sm text-on-surface-variant">Trace loaded successfully. ${eventCount} event${eventCount !== 1 ? 's' : ''} recorded.</p>
      </div>
      <div class="px-3 py-1 bg-surface-container-low border border-outline-variant rounded-full flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-secondary"></span>
        <span class="font-label-uppercase text-label-uppercase text-on-surface-variant">Trace Complete</span>
      </div>`;
  }

  // Timeline events
  const timeline = document.getElementById('trace-timeline');
  if (timeline) {
    const parts = [buildFailedEvent(tx)];
    for (const d of dia) parts.push(buildDiagnosisEvent(d));
    for (const a of act) parts.push(buildActionEvent(a));
    for (const o of out) parts.push(buildOutcomeEvent(o));
    for (const e of exc) parts.push(buildExceptionEvent(e));
    timeline.innerHTML = parts.join('');
  }

  // Show trace container, hide not-found + loading states
  setVisible('trace-container', true);
  setVisible('trace-not-found', false);
  setVisible('trace-loading',   false);
}

// ---------------------------------------------------------------------------
// UI state helpers
// ---------------------------------------------------------------------------

function setVisible(id, visible) {
  const el = document.getElementById(id);
  if (!el) return;
  if (visible) el.classList.remove('hidden');
  else          el.classList.add('hidden');
}

function showLoading() {
  setVisible('trace-loading',   true);
  setVisible('trace-container', false);
  setVisible('trace-not-found', false);
}

function showNotFound(txnId) {
  setVisible('trace-loading',   false);
  setVisible('trace-container', false);
  setVisible('trace-not-found', true);
  const el = document.getElementById('trace-not-found-id');
  if (el) el.textContent = txnId;
}

function showError(message) {
  setVisible('trace-loading',   false);
  setVisible('trace-container', false);
  setVisible('trace-not-found', false);
  const el = document.getElementById('trace-error');
  if (el) {
    el.classList.remove('hidden');
    const msg = el.querySelector('#trace-error-msg');
    if (msg) msg.textContent = message;
  }
}

// ---------------------------------------------------------------------------
// Search logic
// ---------------------------------------------------------------------------

async function runSearch() {
  const input = document.getElementById('txn-search');
  const txnId = (input?.value ?? '').trim();
  if (!txnId) return;

  // Push to URL without reload for deep-link support
  const url = new URL(window.location.href);
  url.searchParams.set('txn', txnId);
  window.history.replaceState({}, '', url.toString());

  showLoading();
  try {
    const data = await fetchTrace(txnId);
    renderTrace(data);
  } catch (err) {
    if (err.status === 404) {
      showNotFound(txnId);
    } else {
      showError(`${err.message}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  const input  = document.getElementById('txn-search');
  const button = document.getElementById('trace-search-btn');

  if (button) button.addEventListener('click', runSearch);
  if (input)  input.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

  // Deep-link: auto-run if ?txn= param present
  const params = new URLSearchParams(window.location.search);
  const deepTxn = params.get('txn');
  if (deepTxn) {
    if (input) input.value = deepTxn;
    runSearch();
  }
});
