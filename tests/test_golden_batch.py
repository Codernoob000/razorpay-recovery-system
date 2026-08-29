"""
tests/test_golden_batch.py
==========================
Golden Regression Suite for the AI Revenue Recovery Platform.

Encodes all 10 core policy and architectural invariants as end-to-end regression
tests using fixed, hand-crafted transactions executed across the complete pipeline
(Detector -> Diagnoser (mocked) -> Strategist -> Executor -> Ledger -> REST API).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-golden",
    "DATABASE_URL": "sqlite:///./test_golden.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.api.app import app, get_diagnoser, get_session
    from recovery_platform.config import get_settings
    from recovery_platform.models import (
        ActionType,
        CustomerTier,
        ExceptionLog,
        FailureCategory,
        Outcome,
        RecoveryAction,
        Transaction,
        TxStatus,
    )
    from recovery_platform.modules.detector import (
        requeue_eligible_retries,
    )
    from recovery_platform.modules.diagnoser import Diagnoser, DiagnosisOutput
    from recovery_platform.modules.executor import Executor
    from recovery_platform.modules.ledger import LedgerPipeline
    from recovery_platform.modules.strategist import (
        RULE_DISCOUNT_ALREADY_OFFERED,
        RULE_DISCOUNT_ELIGIBLE_ENTERPRISE,
        RULE_HARD_DECLINE_UPDATE_LINK,
        RULE_MAX_RETRIES_EXCEEDED,
        RULE_RETRY_ALLOWED,
        RULE_RISK_HOLD_ZERO_RETRY,
        RULE_TECHNICAL_RETRY_ALLOWED,
        Strategist,
    )
    from recovery_platform.seed import seed_database


@pytest.fixture(name="engine")
def engine_fixture():
    """Isolated in-memory SQLite engine per test."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


@pytest.fixture(name="session")
def session_fixture(engine):
    """Active session fixture rolled back per test."""
    with Session(engine) as s:
        yield s
        s.rollback()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _make_tx(
    session: Session,
    status: TxStatus = TxStatus.failed,
    tier: CustomerTier = CustomerTier.starter,
    failure_code: str = "insufficient_funds",
    retry_count: int = 0,
    amount: float = 1000.0,
    offset_minutes: int = 0,
) -> Transaction:
    created = _utcnow() - timedelta(minutes=offset_minutes)
    tx = Transaction(
        id=str(uuid.uuid4()),
        customer_id=f"cust_{uuid.uuid4().hex[:6]}",
        amount=amount,
        type="subscription",
        status=status,
        failure_code=failure_code,
        retry_count=retry_count,
        customer_value_tier=tier,
        created_at=created,
        updated_at=created,
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _make_diagnoser(classification: FailureCategory) -> Diagnoser:
    mock_client = MagicMock()
    mock_client.generate_structured_json.return_value = DiagnosisOutput(
        classification=classification,
        confidence=0.95,
        reasoning=f"Golden test diagnosed {classification.value}",
    )
    return Diagnoser(client=mock_client)


# ===========================================================================
# 1. RULE_RISK_HOLD_ZERO_RETRY
# ===========================================================================


def test_golden_risk_hold_never_retried(session):
    """
    INVARIANT 1: RULE_RISK_HOLD_ZERO_RETRY
    Protects against fraud / high-risk accounts receiving automated retries.
    A risk_hold failure must immediately escalate to human with 0 retries.
    """
    tx = _make_tx(
        session,
        tier=CustomerTier.enterprise,
        failure_code="suspected_fraud",
        retry_count=0,
    )

    diagnoser = _make_diagnoser(FailureCategory.risk_hold)
    strategist = Strategist(policy=get_settings().recovery_policy)
    executor = Executor(policy=get_settings().recovery_policy)
    pipeline = LedgerPipeline()

    summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)
    session.commit()
    session.refresh(tx)

    assert summary["processed"] == 1
    assert summary["escalated_count"] == 1
    assert summary["recovered_count"] == 0
    assert tx.status == TxStatus.escalated
    assert tx.retry_count == 0  # No retries executed

    action = session.exec(select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)).first()
    assert action is not None
    assert action.action_type == ActionType.escalate_to_human
    assert RULE_RISK_HOLD_ZERO_RETRY in action.bounds_applied

    exc_log = session.exec(select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)).first()
    assert exc_log is not None
    assert "risk_hold" in exc_log.reason.lower()


# ===========================================================================
# 2. RULE_MAX_RETRIES_EXCEEDED
# ===========================================================================


def test_golden_max_retries_exceeded_escalates_immediately(session):
    """
    INVARIANT 2: RULE_MAX_RETRIES_EXCEEDED
    Protects against infinite retries.
    A transaction with retry_count >= max_retries must immediately escalate
    regardless of classification.
    """
    policy = get_settings().recovery_policy
    tx = _make_tx(
        session,
        tier=CustomerTier.enterprise,
        failure_code="insufficient_funds",
        retry_count=policy.max_retries,
    )

    diagnoser = _make_diagnoser(FailureCategory.soft_decline)
    strategist = Strategist(policy=policy)
    executor = Executor(policy=policy)
    pipeline = LedgerPipeline()

    summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)
    session.commit()
    session.refresh(tx)

    assert summary["escalated_count"] == 1
    assert tx.status == TxStatus.escalated

    action = session.exec(select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)).first()
    assert action is not None
    assert action.action_type == ActionType.escalate_to_human
    assert RULE_MAX_RETRIES_EXCEEDED in action.bounds_applied


# ===========================================================================
# 3. RULE_HARD_DECLINE_UPDATE_LINK
# ===========================================================================


def test_golden_hard_decline_sends_update_link(session):
    """
    INVARIANT 3: RULE_HARD_DECLINE_UPDATE_LINK
    Protects against retrying unrecoverable payment credentials (expired card, etc.).
    Must route to send_update_link and enter pending_retry.
    """
    tx = _make_tx(session, failure_code="card_expired", retry_count=0)

    diagnoser = _make_diagnoser(FailureCategory.hard_decline)
    strategist = Strategist(policy=get_settings().recovery_policy)
    executor = Executor(policy=get_settings().recovery_policy)
    pipeline = LedgerPipeline()

    pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)
    session.commit()
    session.refresh(tx)

    assert tx.status == TxStatus.pending_retry
    assert tx.retry_count == 1

    action = session.exec(select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)).first()
    assert action is not None
    assert action.action_type == ActionType.send_update_link
    assert RULE_HARD_DECLINE_UPDATE_LINK in action.bounds_applied


# ===========================================================================
# 4. RULE_DISCOUNT_ELIGIBLE_ENTERPRISE + RULE_DISCOUNT_ALREADY_OFFERED
# ===========================================================================


def test_golden_enterprise_discount_offered_once(session):
    """
    INVARIANT 4: RULE_DISCOUNT_ELIGIBLE_ENTERPRISE & RULE_DISCOUNT_ALREADY_OFFERED
    Protects against repetitive discount leakage.
    An enterprise customer with prior failures receives offer_discount on the first
    cycle, but falls back to retry_payment on subsequent cycles if reprocessed.
    """
    policy = get_settings().recovery_policy
    tx = _make_tx(
        session,
        tier=CustomerTier.enterprise,
        failure_code="card_limit_exceeded",
        retry_count=1,
        amount=50000.0,
    )

    diagnoser = _make_diagnoser(FailureCategory.soft_decline)
    strategist = Strategist(policy=policy)

    # Cycle 1: First soft decline on enterprise -> offer_discount
    action1, just1, bounds1 = strategist.evaluate_strategy(tx, diagnoser.diagnose_transaction(tx), session=session)
    assert action1 == ActionType.offer_discount
    assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE in bounds1

    # Persist the action to simulate cycle 1 execution
    rec_action = RecoveryAction(
        transaction_id=tx.id,
        action_type=ActionType.offer_discount,
        justification=just1,
        bounds_applied='["RULE_DISCOUNT_ELIGIBLE_ENTERPRISE"]',
    )
    session.add(rec_action)
    session.commit()

    # Cycle 2: Same transaction diagnosed again -> Guard must intercept and prevent second discount
    action2, just2, bounds2 = strategist.evaluate_strategy(tx, diagnoser.diagnose_transaction(tx), session=session)
    assert action2 == ActionType.retry_payment
    assert RULE_DISCOUNT_ALREADY_OFFERED in bounds2
    assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE not in bounds2


# ===========================================================================
# 5. RULE_RETRY_ALLOWED & TECHNICAL_RETRY_ALLOWED
# ===========================================================================


def test_golden_retry_allowed_for_soft_and_technical(session):
    """
    INVARIANT 5: RULE_RETRY_ALLOWED & RULE_TECHNICAL_RETRY_ALLOWED
    Protects normal recovery throughput for standard soft declines and transient outages.
    """
    tx_soft = _make_tx(session, tier=CustomerTier.starter, failure_code="insufficient_funds")
    tx_tech = _make_tx(session, tier=CustomerTier.business, failure_code="gateway_timeout")

    diagnoser_soft = _make_diagnoser(FailureCategory.soft_decline)
    diagnoser_tech = _make_diagnoser(FailureCategory.technical_failure)
    strategist = Strategist(policy=get_settings().recovery_policy)

    act_soft, _, bounds_soft = strategist.evaluate_strategy(tx_soft, diagnoser_soft.diagnose_transaction(tx_soft), session=session)
    act_tech, _, bounds_tech = strategist.evaluate_strategy(tx_tech, diagnoser_tech.diagnose_transaction(tx_tech), session=session)

    assert act_soft == ActionType.retry_payment
    assert RULE_RETRY_ALLOWED in bounds_soft

    assert act_tech == ActionType.retry_payment
    assert RULE_TECHNICAL_RETRY_ALLOWED in bounds_tech


# ===========================================================================
# 6. TERMINAL-ONLY OUTCOME PERSISTENCE
# ===========================================================================


def test_golden_terminal_only_outcome_persistence(session):
    """
    INVARIANT 6: Terminal-Only Outcome Persistence
    Protects against duplicate Outcome rows and false revenue counting.
    Outcome rows must ONLY be created for terminal states (recovered, escalated, abandoned),
    never for pending_retry.
    """
    tx = _make_tx(session, retry_count=0)
    executor = Executor(policy=get_settings().recovery_policy, seed=0)

    # 1. Non-terminal action (send_update_link -> pending_retry)
    executor.execute_action(session, tx, ActionType.send_update_link, [], "update link")
    session.commit()
    outcomes_pending = session.exec(select(Outcome).where(Outcome.transaction_id == tx.id)).all()
    assert len(outcomes_pending) == 0, "pending_retry must never write an Outcome row!"

    # 2. Terminal action (escalate_to_human -> escalated)
    executor.execute_action(session, tx, ActionType.escalate_to_human, [], "human review")
    session.commit()
    outcomes_terminal = session.exec(select(Outcome).where(Outcome.transaction_id == tx.id)).all()
    assert len(outcomes_terminal) == 1
    assert outcomes_terminal[0].final_status == "escalated"
    assert outcomes_terminal[0].amount_recovered == 0.0


# ===========================================================================
# 7. BOUNDED RETRY LOOP (PAYMENT RETRY & UPDATE LINK)
# ===========================================================================


def test_golden_bounded_retry_loop_payment_retry(session):
    """
    INVARIANT 7A: Bounded Payment Retry Loop
    Protects against infinite retries when payment gateway continues failing.
    Verifies that retry_count strictly increments on each attempt and terminates at escalated.
    """
    policy = get_settings().recovery_policy
    tx = _make_tx(session, failure_code="insufficient_funds", retry_count=0)

    diagnoser = _make_diagnoser(FailureCategory.soft_decline)
    strategist = Strategist(policy=policy)
    executor = Executor(policy=policy)
    executor._rng.random = lambda: 0.99  # Guarantees retry failure to test exhaustion
    pipeline = LedgerPipeline()

    current_time = _utcnow()

    # Iterate through retries up to max_retries
    for _ in range(policy.max_retries):
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, now=current_time)
        session.commit()
        current_time += timedelta(minutes=70)

    session.refresh(tx)
    assert tx.status == TxStatus.escalated
    assert tx.retry_count == policy.max_retries

    outcomes = session.exec(select(Outcome).where(Outcome.transaction_id == tx.id)).all()
    assert len(outcomes) == 1
    assert outcomes[0].final_status == "escalated"


def test_golden_bounded_retry_loop_update_link(session):
    """
    INVARIANT 7B: Bounded Update Link Loop
    Protects against infinite cycling on hard declines.
    Verifies that send_update_link increments retry_count and eventually terminates at escalated.
    """
    policy = get_settings().recovery_policy
    tx = _make_tx(session, failure_code="card_expired", retry_count=0)

    diagnoser = _make_diagnoser(FailureCategory.hard_decline)
    strategist = Strategist(policy=policy)
    executor = Executor(policy=policy)
    pipeline = LedgerPipeline()

    current_time = _utcnow()

    # Cycles 1 to max_retries: send_update_link
    for i in range(1, policy.max_retries + 1):
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, now=current_time)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.pending_retry
        assert tx.retry_count == i
        current_time += timedelta(minutes=70)

    # Final cycle: requeued at max_retries -> Rule 2 escalates
    summary_final = pipeline.run_recovery_cycle(
        session, diagnoser, strategist, executor, now=current_time
    )
    session.commit()
    session.refresh(tx)

    assert summary_final["escalated_count"] == 1
    assert tx.status == TxStatus.escalated
    assert tx.retry_count == policy.max_retries


# ===========================================================================
# 8. REQUEUE DISCIPLINE
# ===========================================================================


def test_golden_requeue_never_modifies_retry_count(session):
    """
    INVARIANT 8: Requeue Discipline
    Protects retry_count integrity during delay requeuing.
    requeue_eligible_retries must transition status from pending_retry back to failed
    WITHOUT touching or resetting retry_count.
    """
    now = _utcnow()
    tx = _make_tx(
        session,
        status=TxStatus.pending_retry,
        retry_count=2,
        offset_minutes=75,
    )

    requeued = requeue_eligible_retries(session, policy=get_settings().recovery_policy, now=now)
    session.commit()
    session.refresh(tx)

    assert requeued == 1
    assert tx.status == TxStatus.failed
    assert tx.retry_count == 2  # retry_count strictly untouched


# ===========================================================================
# 9. SINGLE COMMIT OWNERSHIP (NO STALEDATAERROR)
# ===========================================================================


def test_golden_single_commit_ownership_no_staledataerror(engine):
    """
    INVARIANT 9: Single Commit Ownership
    Protects against SQLAlchemy StaleDataError / false HTTP 500 errors caused
    by intermediate commits during pipeline request lifecycles.
    """
    # Seed 5 failed transactions in test DB
    with Session(engine) as s:
        for _ in range(5):
            _make_tx(s, status=TxStatus.failed)

    mock_client = MagicMock()
    mock_client.generate_structured_json.return_value = DiagnosisOutput(
        classification=FailureCategory.soft_decline,
        confidence=0.9,
        reasoning="Golden test single commit verification",
    )
    mock_diag = Diagnoser(client=mock_client)

    def get_test_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = get_test_session
    app.dependency_overrides[get_diagnoser] = lambda: mock_diag

    client = TestClient(app)
    response = client.post("/pipeline/run")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["processed"] == 5
    assert "requeued_count" in data


# ===========================================================================
# 10. SEED CLEAN-STATE INVARIANT
# ===========================================================================


def test_golden_seed_clean_state_invariant(engine):
    """
    INVARIANT 10: Seed Clean-State Invariant
    Protects initial system state before pipeline execution.
    seed_database() must produce 100% status=failed records, 0 Outcome rows,
    and 0 ExceptionLog rows.
    """
    count = 25
    inserted = seed_database(count=count, reset=True, custom_engine=engine)
    assert inserted == count

    with Session(engine) as s:
        all_txs = s.exec(select(Transaction)).all()
        assert len(all_txs) == count
        assert all(t.status == TxStatus.failed for t in all_txs)

        outcomes = s.exec(select(Outcome)).all()
        assert len(outcomes) == 0

        exc_logs = s.exec(select(ExceptionLog)).all()
        assert len(exc_logs) == 0
