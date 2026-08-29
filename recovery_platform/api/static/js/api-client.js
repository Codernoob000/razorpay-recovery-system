/**
 * api-client.js
 * =============
 * Shared fetch wrapper for the AI Revenue Recovery UI.
 * All paths are same-origin relative (served via FastAPI StaticFiles at /ui/).
 * No framework, no build step — plain ES2020 module syntax loaded via <script>.
 */

// ---------------------------------------------------------------------------
// Typed error class
// ---------------------------------------------------------------------------

class ApiError extends Error {
  /**
   * @param {number} status  HTTP status code
   * @param {string} message Human-readable message
   * @param {any}    body    Parsed response body (may be null)
   */
  constructor(status, message, body = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

// ---------------------------------------------------------------------------
// Core GET / POST wrappers
// ---------------------------------------------------------------------------

/**
 * Perform a GET request and return parsed JSON.
 * @param {string} path  Absolute path on the same origin, e.g. "/metrics"
 * @returns {Promise<any>}
 * @throws {ApiError} on non-2xx responses or network failures
 */
async function apiGet(path) {
  let response;
  try {
    response = await fetch(path, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    });
  } catch (networkErr) {
    throw new ApiError(0, `Network error: ${networkErr.message}`);
  }

  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch (_) { /* ignore */ }
    const detail = body?.detail ?? response.statusText;
    throw new ApiError(response.status, `HTTP ${response.status}: ${detail}`, body);
  }

  return response.json();
}

/**
 * Perform a POST request and return parsed JSON.
 * @param {string} path   Absolute path on the same origin
 * @param {object} [qs]   Query-string key/value pairs
 * @returns {Promise<any>}
 * @throws {ApiError} on non-2xx responses or network failures
 */
async function apiPost(path, qs = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(qs)) {
    if (v !== null && v !== undefined && v !== '') params.set(k, v);
  }
  const url = params.toString() ? `${path}?${params}` : path;

  let response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Accept': 'application/json' },
    });
  } catch (networkErr) {
    throw new ApiError(0, `Network error: ${networkErr.message}`);
  }

  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch (_) { /* ignore */ }
    const detail = body?.detail ?? response.statusText;
    throw new ApiError(response.status, `HTTP ${response.status}: ${detail}`, body);
  }

  return response.json();
}

// ---------------------------------------------------------------------------
// Domain helpers
// ---------------------------------------------------------------------------

/**
 * Fetch aggregate recovery metrics.
 * Maps to GET /metrics → MetricsResponse
 * @returns {Promise<{recovery_rate, total_recovered, total_at_risk,
 *                    mean_time_to_recovery, records_processed, escalated_count}>}
 */
async function fetchMetrics() {
  return apiGet('/metrics');
}

/**
 * Fetch a paginated, filtered list of transactions.
 * Maps to GET /transactions?status=&customer_value_tier=&failure_code=&limit=&offset=
 * @param {{status?: string, customer_value_tier?: string,
 *          failure_code?: string, limit?: number, offset?: number}} [filters]
 * @returns {Promise<Array<TransactionSummary>>}
 */
async function fetchTransactions(filters = {}) {
  const params = new URLSearchParams();
  if (filters.status)               params.set('status', filters.status);
  if (filters.customer_value_tier)  params.set('customer_value_tier', filters.customer_value_tier);
  if (filters.failure_code)         params.set('failure_code', filters.failure_code);
  if (filters.limit)                params.set('limit', filters.limit);
  if (filters.offset)               params.set('offset', filters.offset);
  const qs = params.toString();
  return apiGet(qs ? `/transactions?${qs}` : '/transactions');
}

/**
 * Fetch the full audit trace for a single transaction.
 * Maps to GET /transactions/{transaction_id}/trace → TraceResponse
 * @param {string} transactionId
 * @returns {Promise<TraceResponse>}
 * @throws {ApiError} with status 404 if not found
 */
async function fetchTrace(transactionId) {
  return apiGet(`/transactions/${encodeURIComponent(transactionId)}/trace`);
}

/**
 * Fetch all escalated exceptions.
 * Maps to GET /exceptions → list[ExceptionResponse]
 * @returns {Promise<Array<ExceptionResponse>>}
 */
async function fetchExceptions() {
  return apiGet('/exceptions');
}

/**
 * Trigger one recovery pipeline cycle.
 * Maps to POST /pipeline/run?limit=... → PipelineRunResponse
 * @param {number} [limit=100]
 * @returns {Promise<{processed, recovered_count, escalated_count, total_recovered_amount}>}
 */
async function runPipelineCycle(limit = 100) {
  return apiPost('/pipeline/run', { limit });
}

// ---------------------------------------------------------------------------
// Expose on window so plain <script> tags (non-module) can access helpers
// ---------------------------------------------------------------------------
window.ApiError = ApiError;
window.apiGet = apiGet;
window.apiPost = apiPost;
window.fetchMetrics = fetchMetrics;
window.fetchTransactions = fetchTransactions;
window.fetchTrace = fetchTrace;
window.fetchExceptions = fetchExceptions;
window.runPipelineCycle = runPipelineCycle;
