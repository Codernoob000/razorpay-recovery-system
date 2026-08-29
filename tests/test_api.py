"""
tests/test_api.py
==================
Phase 6 – FastAPI REST layer tests.

All tests use an in-memory SQLite engine injected via FastAPI's
``dependency_overrides``.  The Diagnoser dependency is replaced with a mock
that never calls the Gemini API so tests run offline.

Fixture hierarchy (scope="module" — one DB per test session):
    test_engine   – bare in-memory SQLite engine with schema created
    seeded_engine – test_engine populated with controlled test data
    client        – TestClient with get_session and get_diagnoser overridden

Fixed transaction IDs guarantee deterministic policy-invariant assertions:
    RISK_HOLD_TX_ID    – escalated, has ExceptionLog, must never be "recovered"
    MAX_RETRY_TX_ID    – retry_count >= max_retries (3), escalated
    RECOVERED_TX_ID    – status=recovered, has Diagnosis + Outcome + RecoveryAction
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Environment must be set BEFORE any recovery_platform imports so that
# get_settings() (lru_cache) is first called with valid values.
# ---------------------------------------------------------------------------

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-api-phase6",
    "DATABASE_URL":   "sqlite:///./test_api_phase6.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.api.app import app, format_mean_time_to_recovery, get_diagnoser
    from recovery_platform.database import get_session
    from recovery_platform.models import (
        ActionType,
        CustomerTier,
        Diagnosis,
        ExceptionLog,
        FailureCategory,
        Outcome,
        RecoveryAction,
        Transaction,
        TxStatus,
    )
    from recovery_platform.modules.diagnoser import Diagnoser, DiagnosisOutput
    from recovery_platform.seed import generate_synthetic_transactions

# ---------------------------------------------------------------------------
# Fixed test IDs (deterministic, not from seed randomness)
# ---------------------------------------------------------------------------

RISK_HOLD_TX_ID = "tx-risk-hold-phase6-001"
MAX_RETRY_TX_ID = "tx-max-retry-phase6-001"
RECOVERED_TX_ID = "tx-recovered-phase6-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _naive(dt: datetime) -> datetime:
    """Strip timezone for SQLite compatibility in comparisons."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_engine():
    """In-memory SQLite engine with schema created once for the module."""
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Import models explicitly so SQLModel.metadata is fully populated
    # before create_all — the order of module-level imports is not guaranteed.
    import recovery_platform.models  # noqa: F401 (side-effect import)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(scope="module")
def seeded_engine(test_engine):
    """
    Populate the in-memory engine with:
      1. 25 randomly-generated Transaction rows (all status=failed)
      2. Three fixed transactions with controlled status and related rows
         so policy-invariant assertions can use deterministic IDs.
    """
    past = _utcnow() - timedelta(hours=2)
    now = _utcnow()

    with Session(test_engine) as s:
        # --- 1. Random base transactions (for list/filter/pagination tests) --
        for tx in generate_synthetic_transactions(count=25):
            s.add(tx)

        # --- 2. Fixed: RISK_HOLD transaction (escalated) ---------------------
        s.add(Transaction(
            id=RISK_HOLD_TX_ID,
            customer_id="cust-risk-001",
            amount=8_500.0,
            type="subscription",
            status=TxStatus.escalated,
            failure_code="suspected_fraud",
            retry_count=0,
            customer_value_tier=CustomerTier.enterprise,
            created_at=past,
            updated_at=now,
        ))

        # --- 3. Fixed: MAX_RETRY transaction (escalated, retry_count == 3) ---
        s.add(Transaction(
            id=MAX_RETRY_TX_ID,
            customer_id="cust-maxretry-001",
            amount=1_299.0,
            type="subscription",
            status=TxStatus.escalated,
            failure_code="insufficient_funds",
            retry_count=3,
            customer_value_tier=CustomerTier.starter,
            created_at=past,
            updated_at=now,
        ))

        # --- 4. Fixed: RECOVERED transaction ---------------------------------
        s.add(Transaction(
            id=RECOVERED_TX_ID,
            customer_id="cust-recovered-001",
            amount=6_000.0,
            type="subscription",
            status=TxStatus.recovered,
            failure_code="insufficient_funds",
            retry_count=1,
            customer_value_tier=CustomerTier.enterprise,
            created_at=past,
            updated_at=now,
        ))

        s.commit()

        # --- Diagnosis rows --------------------------------------------------
        s.add(Diagnosis(
            transaction_id=RISK_HOLD_TX_ID,
            classification=FailureCategory.risk_hold,
            confidence=0.97,
            reasoning=(
                "Velocity limit exceeded within 5-minute window. "
                "Transaction flagged as suspected fraud by AML engine."
            ),
            created_at=past,
        ))
        s.add(Diagnosis(
            transaction_id=RECOVERED_TX_ID,
            classification=FailureCategory.soft_decline,
            confidence=0.91,
            reasoning=(
                "Insufficient funds at time of transaction; "
                "customer topped up the account before the retry window."
            ),
            created_at=past,
        ))

        # --- RecoveryAction rows ---------------------------------------------
        s.add(RecoveryAction(
            transaction_id=RISK_HOLD_TX_ID,
            action_type=ActionType.escalate_to_human,
            justification=(
                f"Transaction {RISK_HOLD_TX_ID} classified as risk_hold "
                f"(confidence=0.97). Policy mandates immediate human escalation."
            ),
            bounds_applied=json.dumps(["RULE_RISK_HOLD_ZERO_RETRY"]),
            created_at=past,
        ))
        s.add(RecoveryAction(
            transaction_id=RECOVERED_TX_ID,
            action_type=ActionType.offer_discount,
            justification=(
                f"Transaction {RECOVERED_TX_ID}: enterprise customer with "
                f"1 prior attempt. Offering discount up to 15% to recover revenue."
            ),
            bounds_applied=json.dumps(["RULE_DISCOUNT_ELIGIBLE_ENTERPRISE"]),
            created_at=past,
        ))

        # --- Outcome rows ----------------------------------------------------
        s.add(Outcome(
            transaction_id=RISK_HOLD_TX_ID,
            final_status="escalated",
            amount_recovered=0.0,
            resolved_at=now,
        ))
        s.add(Outcome(
            transaction_id=RECOVERED_TX_ID,
            final_status="recovered",
            amount_recovered=5_100.0,
            resolved_at=now,
        ))

        # --- ExceptionLog row (risk_hold only) --------------------------------
        s.add(ExceptionLog(
            transaction_id=RISK_HOLD_TX_ID,
            reason=(
                "Velocity limit exceeded within 5-minute window. "
                "Transaction flagged as suspected fraud by AML engine."
            ),
            escalated_at=now,
        ))

        s.commit()

    return test_engine


@pytest.fixture(scope="module")
def mock_diagnoser():
    """
    Diagnoser backed by a MagicMock client.

    Always returns a soft_decline DiagnosisOutput so the pipeline can run
    without real Gemini API calls.
    """
    mock_client = MagicMock()
    mock_client.generate_structured_json.return_value = DiagnosisOutput(
        classification=FailureCategory.soft_decline,
        confidence=0.85,
        reasoning="Mock LLM diagnosis for offline testing.",
    )
    return Diagnoser(client=mock_client)


@pytest.fixture(scope="module")
def client(seeded_engine, mock_diagnoser):
    """
    TestClient with two dependency overrides:
      - get_session  -> yields from the in-memory seeded_engine
      - get_diagnoser -> returns mock_diagnoser (no real Gemini calls)
    """
    def _override_session():
        with Session(seeded_engine) as s:
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise

    app.dependency_overrides[get_session]   = _override_session
    app.dependency_overrides[get_diagnoser] = lambda: mock_diagnoser

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------


class TestListTransactions:
    def test_returns_list(self, client):
        r = client.get("/transactions")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_default_limit_respected(self, client):
        r = client.get("/transactions?limit=5")
        assert r.status_code == 200
        assert len(r.json()) <= 5

    def test_offset_paginates(self, client):
        page1 = client.get("/transactions?limit=3&offset=0").json()
        page2 = client.get("/transactions?limit=3&offset=3").json()
        ids1 = {tx["id"] for tx in page1}
        ids2 = {tx["id"] for tx in page2}
        assert ids1.isdisjoint(ids2), "Pagination must not return overlapping pages"

    def test_filter_by_status_escalated(self, client):
        r = client.get("/transactions?status=escalated")
        assert r.status_code == 200
        body = r.json()
        assert len(body) >= 2  # at least RISK_HOLD and MAX_RETRY fixed txs
        for tx in body:
            assert tx["status"] == "escalated"

    def test_filter_by_status_recovered(self, client):
        r = client.get("/transactions?status=recovered")
        assert r.status_code == 200
        body = r.json()
        ids = [tx["id"] for tx in body]
        assert RECOVERED_TX_ID in ids

    def test_filter_by_customer_tier(self, client):
        r = client.get("/transactions?customer_value_tier=enterprise")
        assert r.status_code == 200
        for tx in r.json():
            assert tx["customer_value_tier"] == "enterprise"

    def test_filter_by_failure_code(self, client):
        r = client.get("/transactions?failure_code=suspected_fraud")
        assert r.status_code == 200
        body = r.json()
        ids = [tx["id"] for tx in body]
        assert RISK_HOLD_TX_ID in ids

    def test_invalid_status_returns_400(self, client):
        r = client.get("/transactions?status=not_a_real_status")
        assert r.status_code == 400

    def test_invalid_tier_returns_400(self, client):
        r = client.get("/transactions?customer_value_tier=premium")
        assert r.status_code == 400

    def test_response_schema_keys(self, client):
        r = client.get("/transactions?limit=1")
        assert r.status_code == 200
        body = r.json()
        if body:
            tx = body[0]
            for key in ("id", "customer_id", "amount", "status",
                        "failure_code", "retry_count", "customer_value_tier",
                        "created_at", "updated_at"):
                assert key in tx, f"Missing key {key!r} in TransactionSummary"


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_status_200(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_all_keys_present(self, client):
        body = client.get("/metrics").json()
        required = {
            "recovery_rate", "total_recovered", "total_at_risk",
            "mean_time_to_recovery", "records_processed", "escalated_count",
        }
        assert required <= body.keys()

    def test_recovery_rate_in_range(self, client):
        body = client.get("/metrics").json()
        assert 0.0 <= body["recovery_rate"] <= 1.0

    def test_records_processed_positive(self, client):
        body = client.get("/metrics").json()
        assert body["records_processed"] >= 28  # 25 seeded + 3 fixed

    def test_escalated_count_at_least_two(self, client):
        body = client.get("/metrics").json()
        assert body["escalated_count"] >= 2  # RISK_HOLD + MAX_RETRY fixed txs

    def test_total_recovered_non_negative(self, client):
        body = client.get("/metrics").json()
        assert body["total_recovered"] >= 5_100.0  # at least RECOVERED_TX outcome

    def test_mean_time_to_recovery_present_or_null(self, client):
        body = client.get("/metrics").json()
        mtr = body["mean_time_to_recovery"]
        assert mtr is None or (isinstance(mtr, (int, float)) and mtr >= 0)

    def test_mean_time_to_recovery_is_positive_seconds(self, client):
        """With RECOVERED_TX seeded 2 hours in the past, MTR should be ~7200s."""
        body = client.get("/metrics").json()
        mtr = body["mean_time_to_recovery"]
        if mtr is not None:
            assert mtr > 0, "MTR must be positive when recoveries exist"

    def test_mean_time_to_recovery_formatted_present(self, client):
        body = client.get("/metrics").json()
        assert "mean_time_to_recovery_formatted" in body
        mtr_fmt = body["mean_time_to_recovery_formatted"]
        assert mtr_fmt is not None
        assert isinstance(mtr_fmt, str)

    def test_format_mean_time_to_recovery_helper(self):
        assert format_mean_time_to_recovery(None) is None
        assert format_mean_time_to_recovery(-5.0) is None
        assert format_mean_time_to_recovery(45.0) == "45s"
        assert format_mean_time_to_recovery(720.0) == "12 min"
        assert format_mean_time_to_recovery(360.0) == "6.0 min"
        assert format_mean_time_to_recovery(15480.0) == "4.3 hrs"
        assert format_mean_time_to_recovery(3600.0) == "1.0 hr"
        assert format_mean_time_to_recovery(86400.0) == "1.0 day"
        assert format_mean_time_to_recovery(260441.34) == "3.0 days"



# ---------------------------------------------------------------------------
# GET /transactions/{id}/trace
# ---------------------------------------------------------------------------


class TestTrace:
    def test_known_transaction_200(self, client):
        r = client.get(f"/transactions/{RECOVERED_TX_ID}/trace")
        assert r.status_code == 200

    def test_unknown_transaction_404(self, client):
        r = client.get("/transactions/does-not-exist-ever/trace")
        assert r.status_code == 404
        body = r.json()
        assert "detail" in body

    def test_trace_schema_keys(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        for key in ("transaction", "diagnoses", "recovery_actions",
                    "outcomes", "exception_logs"):
            assert key in body, f"Missing key {key!r} in TraceResponse"

    def test_transaction_id_matches(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert body["transaction"]["id"] == RECOVERED_TX_ID

    def test_diagnosis_present_with_full_reasoning(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert len(body["diagnoses"]) == 1
        d = body["diagnoses"][0]
        assert d["classification"] == "soft_decline"
        assert d["confidence"] == pytest.approx(0.91, abs=1e-6)
        # Reasoning is verbatim and not truncated
        assert "insufficient funds" in d["reasoning"].lower()

    def test_recovery_action_present_with_full_justification(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert len(body["recovery_actions"]) >= 1
        action = body["recovery_actions"][0]
        assert action["action_type"] == "offer_discount"
        assert isinstance(action["bounds_applied"], list)
        assert "RULE_DISCOUNT_ELIGIBLE_ENTERPRISE" in action["bounds_applied"]
        # Justification is untruncated
        assert len(action["justification"]) > 0

    def test_outcome_present(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert len(body["outcomes"]) == 1
        outcome = body["outcomes"][0]
        assert outcome["final_status"] == "recovered"
        assert outcome["amount_recovered"] == pytest.approx(5_100.0, abs=0.01)

    def test_no_exception_log_for_recovered_tx(self, client):
        body = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert body["exception_logs"] == []

    def test_risk_hold_trace_has_exception_log(self, client):
        body = client.get(f"/transactions/{RISK_HOLD_TX_ID}/trace").json()
        assert len(body["exception_logs"]) == 1
        assert "fraud" in body["exception_logs"][0]["reason"].lower()


# ---------------------------------------------------------------------------
# GET /exceptions
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_status_200(self, client):
        r = client.get("/exceptions")
        assert r.status_code == 200

    def test_returns_list(self, client):
        assert isinstance(client.get("/exceptions").json(), list)

    def test_risk_hold_tx_present(self, client):
        body = client.get("/exceptions").json()
        tx_ids = [e["transaction_id"] for e in body]
        assert RISK_HOLD_TX_ID in tx_ids

    def test_exception_schema_keys(self, client):
        body = client.get("/exceptions").json()
        assert len(body) >= 1
        e = next(ex for ex in body if ex["transaction_id"] == RISK_HOLD_TX_ID)
        for key in ("id", "transaction_id", "customer_id", "amount",
                    "failure_code", "customer_value_tier", "reason", "escalated_at"):
            assert key in e, f"Missing key {key!r} in ExceptionResponse"

    def test_transaction_context_joined_correctly(self, client):
        body = client.get("/exceptions").json()
        e = next(ex for ex in body if ex["transaction_id"] == RISK_HOLD_TX_ID)
        assert e["failure_code"] == "suspected_fraud"
        assert e["customer_value_tier"] == "enterprise"
        assert e["amount"] == pytest.approx(8_500.0, abs=0.01)

    def test_sorted_by_reason(self, client):
        body = client.get("/exceptions").json()
        reasons = [e["reason"] for e in body]
        assert reasons == sorted(reasons), "Exceptions must be sorted by reason ascending"


# ---------------------------------------------------------------------------
# POST /pipeline/run
# ---------------------------------------------------------------------------


class TestPipelineRun:
    def test_status_200(self, client):
        r = client.post("/pipeline/run")
        assert r.status_code == 200

    def test_response_schema_keys(self, client):
        body = client.post("/pipeline/run").json()
        for key in ("processed", "recovered_count", "escalated_count",
                    "total_recovered_amount"):
            assert key in body, f"Missing key {key!r} in PipelineRunResponse"

    def test_all_values_are_numeric(self, client):
        body = client.post("/pipeline/run").json()
        assert isinstance(body["processed"], int)
        assert isinstance(body["recovered_count"], int)
        assert isinstance(body["escalated_count"], int)
        assert isinstance(body["total_recovered_amount"], (int, float))

    def test_counts_are_non_negative(self, client):
        body = client.post("/pipeline/run").json()
        assert body["processed"] >= 0
        assert body["recovered_count"] >= 0
        assert body["escalated_count"] >= 0
        assert body["total_recovered_amount"] >= 0.0

    def test_limit_query_param_accepted(self, client):
        r = client.post("/pipeline/run?limit=5")
        assert r.status_code == 200

    def test_invalid_limit_returns_422(self, client):
        r = client.post("/pipeline/run?limit=0")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Policy-invariant assertions
# ---------------------------------------------------------------------------


class TestPolicyInvariants:
    """
    Cross-endpoint invariants derived directly from the recovery policy.
    These assertions constitute the buildathon correctness contract.
    """

    def test_risk_hold_appears_in_exceptions(self, client):
        """
        INVARIANT: Any transaction classified as risk_hold must appear in the
        /exceptions endpoint.  Risk holds are never silently discarded.
        """
        body = client.get("/exceptions").json()
        tx_ids = {e["transaction_id"] for e in body}
        assert RISK_HOLD_TX_ID in tx_ids, (
            "risk_hold transaction must be present in /exceptions"
        )

    def test_risk_hold_never_shows_recovered_status(self, client):
        """
        INVARIANT: A risk_hold transaction must never reach 'recovered' status.
        Check via both the trace endpoint and the transaction list.
        """
        # Via trace
        trace = client.get(f"/transactions/{RISK_HOLD_TX_ID}/trace").json()
        tx_status = trace["transaction"]["status"]
        assert tx_status != "recovered", (
            f"risk_hold tx status is {tx_status!r}, expected 'escalated'"
        )
        for outcome in trace["outcomes"]:
            assert outcome["final_status"] != "recovered", (
                "Outcome.final_status must not be 'recovered' for a risk_hold tx"
            )

        # Via list filter
        recovered_list = client.get("/transactions?status=recovered").json()
        recovered_ids = {tx["id"] for tx in recovered_list}
        assert RISK_HOLD_TX_ID not in recovered_ids, (
            "risk_hold tx must not appear in status=recovered list"
        )

    def test_max_retry_transaction_is_escalated_not_pending(self, client):
        """
        INVARIANT: A transaction at retry_count >= max_retries must be escalated,
        not left as pending_retry.
        """
        r = client.get(f"/transactions/{MAX_RETRY_TX_ID}/trace")
        assert r.status_code == 200
        trace = r.json()
        tx_status = trace["transaction"]["status"]
        assert tx_status == "escalated", (
            f"Max-retry tx status is {tx_status!r}, expected 'escalated'"
        )
        assert tx_status != "pending_retry", (
            "Max-retry tx must not remain as pending_retry"
        )

    def test_max_retry_tx_retry_count_matches_policy(self, client):
        """The seeded max-retry tx has retry_count == 3 (policy max_retries)."""
        trace = client.get(f"/transactions/{MAX_RETRY_TX_ID}/trace").json()
        assert trace["transaction"]["retry_count"] >= 3

    def test_recovered_tx_has_positive_amount_recovered(self, client):
        """
        INVARIANT: A recovered transaction must have a positive amount_recovered
        in its Outcome, not zero.
        """
        trace = client.get(f"/transactions/{RECOVERED_TX_ID}/trace").json()
        assert len(trace["outcomes"]) >= 1
        for o in trace["outcomes"]:
            if o["final_status"] == "recovered":
                assert o["amount_recovered"] > 0, (
                    "Recovered outcome must have amount_recovered > 0"
                )

    def test_recovered_tx_not_in_exceptions(self, client):
        """
        INVARIANT: A successfully recovered transaction must NOT appear in
        /exceptions (no false-positive escalations).
        """
        body = client.get("/exceptions").json()
        tx_ids = {e["transaction_id"] for e in body}
        assert RECOVERED_TX_ID not in tx_ids, (
            "Recovered tx must not appear in /exceptions"
        )
