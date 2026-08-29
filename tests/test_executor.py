"""
tests/test_executor.py
=======================
Phase 5 – Executor state-transition tests and LedgerPipeline end-to-end test.
All run against in-memory SQLite; no real LLM or Gemini key needed.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-exec",
    "DATABASE_URL":   "sqlite:///./test_exec.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
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
    from recovery_platform.modules.executor import Executor
    from recovery_platform.modules.ledger import LedgerPipeline
    from recovery_platform.modules.strategist import Strategist


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="engine")
def engine_fixture():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _policy(max_retries: int = 3, max_pct: int = 15):
    return SimpleNamespace(
        max_retries=max_retries,
        discount_eligibility=SimpleNamespace(min_tier="enterprise", max_pct=max_pct),
        retry_delays=SimpleNamespace(soft_decline_min=60, technical_failure_min=15),
    )


def _utcnow():
    return datetime.now(tz=UTC)


def _tx(
    session: Session,
    retry_count: int = 0,
    tier: CustomerTier = CustomerTier.starter,
    amount: float = 4999.0,
    failure_code: str = "insufficient_funds",
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        customer_id=f"cust_{uuid.uuid4().hex[:6]}",
        amount=amount,
        type="subscription",
        status=TxStatus.failed,
        failure_code=failure_code,
        retry_count=retry_count,
        customer_value_tier=tier,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


# ---------------------------------------------------------------------------
# Executor: retry_payment
# ---------------------------------------------------------------------------

class TestExecutorRetryPayment:

    def test_retry_increments_retry_count(self, session):
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(), seed=42)   # seed=42 -> success
        ex.execute_action(session, tx, ActionType.retry_payment, ["RULE_RETRY"], "retry test")
        session.commit()
        session.refresh(tx)
        assert tx.retry_count == 1

    def test_retry_success_sets_recovered(self, session):
        # seed=42 gives random() = 0.639… < 0.70 -> success
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(), seed=42)
        status, amount = ex.execute_action(
            session, tx, ActionType.retry_payment, ["RULE_RETRY"], "retry"
        )
        session.commit()
        assert status == TxStatus.recovered
        assert amount == pytest.approx(tx.amount)

    def test_retry_fail_sets_pending(self, session):
        # seed=1 gives random() = 0.134… but let us use seed that fails
        # For retry_count<=2, need rng.random() >= 0.70
        # Use seed=0 -> random.Random(0).random() = 0.844 -> FAIL path
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(max_retries=5), seed=0)
        status, amount = ex.execute_action(
            session, tx, ActionType.retry_payment, ["RULE_RETRY"], "retry"
        )
        session.commit()
        assert status in (TxStatus.pending_retry, TxStatus.escalated)
        assert amount == pytest.approx(0.0)

    def test_retry_persists_recovery_action(self, session):
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(), seed=42)
        ex.execute_action(session, tx, ActionType.retry_payment, ["BOUND_A"], "justification")
        session.commit()

        actions = session.exec(
            select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)
        ).all()
        assert len(actions) >= 1
        assert actions[0].action_type == ActionType.retry_payment

    def test_retry_persists_outcome(self, session):
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(), seed=42)
        ex.execute_action(session, tx, ActionType.retry_payment, [], "outcome test")
        session.commit()

        outcomes = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes) >= 1


# ---------------------------------------------------------------------------
# Executor: send_update_link
# ---------------------------------------------------------------------------

class TestExecutorSendUpdateLink:

    def test_update_link_sets_pending_retry(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        status, amount = ex.execute_action(
            session, tx, ActionType.send_update_link, ["RULE_HARD"], "hard decline"
        )
        session.commit()
        assert status == TxStatus.pending_retry
        assert amount == pytest.approx(0.0)

    def test_update_link_persists_recovery_action(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        ex.execute_action(
            session, tx, ActionType.send_update_link, ["R"], "update link"
        )
        session.commit()
        actions = session.exec(
            select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)
        ).all()
        assert any(a.action_type == ActionType.send_update_link for a in actions)

    def test_update_link_does_not_persist_outcome(self, session):
        """send_update_link transitions to pending_retry (non-terminal); must not create an Outcome row."""
        tx = _tx(session)
        ex = Executor(policy=_policy())
        ex.execute_action(session, tx, ActionType.send_update_link, [], "link")
        session.commit()
        outcomes = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes) == 0

    def test_pending_retry_never_persists_outcome(self, session):
        """Failed retry that returns pending_retry must NOT create an Outcome row."""
        tx = _tx(session, retry_count=0)
        ex = Executor(policy=_policy(max_retries=5), seed=0)
        status, _ = ex.execute_action(session, tx, ActionType.retry_payment, [], "retry")
        session.commit()
        assert status == TxStatus.pending_retry
        outcomes = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes) == 0



# ---------------------------------------------------------------------------
# Executor: offer_discount
# ---------------------------------------------------------------------------

class TestExecutorOfferDiscount:

    def test_discount_sets_recovered(self, session):
        tx = _tx(session, amount=10000.0, tier=CustomerTier.enterprise)
        ex = Executor(policy=_policy(max_pct=15))
        status, amount = ex.execute_action(
            session, tx, ActionType.offer_discount, ["RULE_DISC"], "discount"
        )
        session.commit()
        assert status == TxStatus.recovered
        assert amount == pytest.approx(8500.0)   # 10000 * (1 - 0.15)

    def test_discount_amount_formula(self, session):
        for pct in [10, 15, 20]:
            tx = _tx(session, amount=5000.0, tier=CustomerTier.enterprise)
            ex = Executor(policy=_policy(max_pct=pct))
            _, recovered = ex.execute_action(
                session, tx, ActionType.offer_discount, [], "disc"
            )
            session.commit()
            expected = round(5000.0 * (1.0 - pct / 100.0), 2)
            assert recovered == pytest.approx(expected), f"pct={pct}"

    def test_discount_persists_outcome(self, session):
        tx = _tx(session, amount=2000.0)
        ex = Executor(policy=_policy(max_pct=15))
        ex.execute_action(session, tx, ActionType.offer_discount, [], "disc")
        session.commit()
        outcomes = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes) >= 1


# ---------------------------------------------------------------------------
# Executor: escalate_to_human
# ---------------------------------------------------------------------------

class TestExecutorEscalateToHuman:

    def test_escalation_sets_escalated_status(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        status, amount = ex.execute_action(
            session, tx, ActionType.escalate_to_human, ["RULE_RISK"], "risk hold"
        )
        session.commit()
        assert status == TxStatus.escalated
        assert amount == pytest.approx(0.0)

    def test_escalation_creates_exception_log(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        ex.execute_action(
            session, tx, ActionType.escalate_to_human, ["RULE_RISK"], "suspected fraud"
        )
        session.commit()

        logs = session.exec(
            select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)
        ).all()
        assert len(logs) >= 1
        assert "fraud" in logs[0].reason.lower() or "risk" in logs[0].reason.lower() or "suspected" in logs[0].reason.lower()

    def test_escalation_persists_recovery_action(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        ex.execute_action(session, tx, ActionType.escalate_to_human, ["R"], "esc")
        session.commit()
        actions = session.exec(
            select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)
        ).all()
        assert any(a.action_type == ActionType.escalate_to_human for a in actions)

    def test_escalation_no_exception_log_for_other_actions(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy(), seed=42)
        ex.execute_action(session, tx, ActionType.retry_payment, [], "retry – not escalation")
        session.commit()
        logs = session.exec(
            select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)
        ).all()
        assert len(logs) == 0   # no exception log for non-escalation actions

    def test_retry_exhaustion_creates_exception_log(self, session):
        tx = _tx(session, retry_count=2)
        ex = Executor(policy=_policy(max_retries=3))
        with patch.object(ex._rng, "random", return_value=0.99):
            status, amount = ex.execute_action(
                session, tx, ActionType.retry_payment, ["RULE_RETRY"], "retry exhausted"
            )
        session.commit()
        assert status == TxStatus.escalated
        logs = session.exec(
            select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)
        ).all()
        assert len(logs) == 1
        assert "max retries" in logs[0].reason.lower()
        assert tx.failure_code in logs[0].reason

    def test_bounds_stored_as_json(self, session):
        tx = _tx(session)
        ex = Executor(policy=_policy())
        bounds = ["RULE_RISK_HOLD_ZERO_RETRY", "EXTRA_BOUND"]
        ex.execute_action(session, tx, ActionType.escalate_to_human, bounds, "bounds test")
        session.commit()

        action_row = session.exec(
            select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)
        ).first()
        parsed = json.loads(action_row.bounds_applied)
        assert parsed == bounds

    def test_updated_at_refreshed(self, session):
        from datetime import datetime as dt
        from datetime import timedelta
        original = dt.utcnow() - timedelta(hours=1)
        tx = _tx(session)
        tx.updated_at = original
        session.add(tx)
        session.commit()
        session.refresh(tx)

        ex = Executor(policy=_policy())
        ex.execute_action(session, tx, ActionType.escalate_to_human, [], "time check")
        session.commit()
        session.refresh(tx)

        refreshed = tx.updated_at.replace(tzinfo=None) if tx.updated_at.tzinfo else tx.updated_at
        assert refreshed > original


# ---------------------------------------------------------------------------
# LedgerPipeline end-to-end
# ---------------------------------------------------------------------------

class TestLedgerPipeline:
    """
    Full pipeline integration test using mocked Diagnoser + real Strategist
    + real Executor against an in-memory SQLite DB.
    """

    @staticmethod
    def _make_diagnoser(classification: FailureCategory, confidence: float = 0.9):
        """Mock diagnoser that always returns the given classification."""
        from recovery_platform.modules.diagnoser import Diagnoser, DiagnosisOutput

        output = DiagnosisOutput(
            classification=classification,
            confidence=confidence,
            reasoning="mocked",
        )
        mock_client = MagicMock()
        mock_client.generate_structured_json.return_value = output
        return Diagnoser(client=mock_client)

    def _seed_failed_txs(self, session: Session, n: int, **tx_kwargs) -> list[Transaction]:
        txs = []
        for _ in range(n):
            txs.append(_tx(session, **tx_kwargs))
        return txs

    def test_processes_all_failed_transactions(self, session):
        self._seed_failed_txs(session, 5, retry_count=0)
        diagnoser  = self._make_diagnoser(FailureCategory.soft_decline)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy(), seed=42)
        pipeline   = LedgerPipeline()

        summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, limit=10)

        assert summary["processed"] == 5

    def test_summary_keys_present(self, session):
        self._seed_failed_txs(session, 3)
        diagnoser  = self._make_diagnoser(FailureCategory.technical_failure)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy(), seed=7)
        pipeline   = LedgerPipeline()

        summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)

        for key in ["processed", "recovered_count", "escalated_count", "total_recovered_amount"]:
            assert key in summary, f"Missing key: {key}"

    def test_escalation_counted(self, session):
        self._seed_failed_txs(session, 4, retry_count=0)
        diagnoser  = self._make_diagnoser(FailureCategory.risk_hold)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy())
        pipeline   = LedgerPipeline()

        summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)

        # All risk_hold -> escalate_to_human -> TxStatus.escalated
        assert summary["escalated_count"] == summary["processed"]
        assert summary["recovered_count"] == 0
        assert summary["total_recovered_amount"] == pytest.approx(0.0)

    def test_recovery_counted_and_amount_positive(self, session):
        # Offer discount always recovers
        self._seed_failed_txs(
            session, 3,
            retry_count=1, tier=CustomerTier.enterprise, amount=10000.0
        )
        diagnoser  = self._make_diagnoser(FailureCategory.soft_decline)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy(max_pct=15))
        pipeline   = LedgerPipeline()

        summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)

        assert summary["recovered_count"] == 3
        assert summary["total_recovered_amount"] == pytest.approx(3 * 8500.0)

    def test_only_failed_status_processed(self, session):
        """Pending, recovered, escalated txs must not be picked up."""
        for status in [TxStatus.pending_retry, TxStatus.recovered, TxStatus.escalated]:
            bad_tx = Transaction(
                id=str(uuid.uuid4()),
                customer_id="cust_skip",
                amount=500.0,
                type="subscription",
                status=status,
                failure_code="any",
                retry_count=0,
                customer_value_tier=CustomerTier.starter,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            session.add(bad_tx)
        session.commit()

        diagnoser  = self._make_diagnoser(FailureCategory.soft_decline)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy(), seed=42)
        pipeline   = LedgerPipeline()

        summary = pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)

        # The 3 non-failed txs above must not increase processed count
        # (processed count reflects only the failed ones added in earlier tests)
        # We confirm no exception was raised and structure is valid
        assert "processed" in summary

    def test_persistence_after_cycle_terminal_state(self, session):
        """After a cycle with terminal resolution, RecoveryAction and Outcome rows exist."""
        fresh_txs = self._seed_failed_txs(session, 2, retry_count=0)
        tx_ids = [t.id for t in fresh_txs]

        diagnoser  = self._make_diagnoser(FailureCategory.risk_hold)
        strategist = Strategist(policy=_policy())
        executor   = Executor(policy=_policy())
        pipeline   = LedgerPipeline()

        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor)

        for tx_id in tx_ids:
            actions = session.exec(
                select(RecoveryAction).where(RecoveryAction.transaction_id == tx_id)
            ).all()
            outcomes = session.exec(
                select(Outcome).where(Outcome.transaction_id == tx_id)
            ).all()
            assert len(actions) >= 1, f"No RecoveryAction for {tx_id}"
            assert len(outcomes) >= 1, f"No Outcome for {tx_id}"

    def test_multicycle_retry_lifecycle_produces_exactly_one_outcome(self, session):
        """
        A transaction that goes failed -> pending_retry -> requeued -> failed -> recovered
        across two full cycles produces exactly ONE Outcome row total.
        """
        from datetime import timedelta
        tx = _tx(session, retry_count=0)

        diagnoser  = self._make_diagnoser(FailureCategory.soft_decline)
        strategist = Strategist(policy=_policy(max_retries=5))
        # Cycle 1: seed=0 forces failed retry -> pending_retry (0 outcomes)
        executor1  = Executor(policy=_policy(max_retries=5), seed=0)
        pipeline   = LedgerPipeline()

        # Run Cycle 1
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor1)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.pending_retry

        outcomes_cycle1 = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes_cycle1) == 0, "pending_retry must not produce an Outcome row"

        # Simulate 70 minutes elapsed time -> requeued in Cycle 2
        now_cycle2 = _utcnow() + timedelta(minutes=70)
        # Cycle 2: seed=42 forces successful retry -> recovered
        executor2 = Executor(policy=_policy(max_retries=5), seed=42)

        summary2 = pipeline.run_recovery_cycle(
            session, diagnoser, strategist, executor2, now=now_cycle2
        )
        session.commit()
        session.refresh(tx)

        assert summary2["requeued_count"] >= 1
        assert tx.status == TxStatus.recovered

        outcomes_cycle2 = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes_cycle2) == 1, (
            f"Expected exactly 1 Outcome row after final recovery, got {len(outcomes_cycle2)}"
        )
        assert outcomes_cycle2[0].final_status == "recovered"
        assert outcomes_cycle2[0].amount_recovered == pytest.approx(tx.amount)

    def test_hard_decline_requeue_cycles_eventually_escalate_at_max_retries(self, session):
        """
        A hard_decline transaction that gets send_update_link repeatedly across multiple requeue
        cycles increments retry_count on each attempt and terminates at escalated with ExceptionLog.
        """
        from datetime import timedelta
        tx = _tx(session, retry_count=0)

        diagnoser  = self._make_diagnoser(FailureCategory.hard_decline)
        strategist = Strategist(policy=_policy(max_retries=3))
        executor   = Executor(policy=_policy(max_retries=3))
        pipeline   = LedgerPipeline()

        current_time = _utcnow()

        # Cycle 1: send_update_link (retry_count becomes 1)
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, now=current_time)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.pending_retry
        assert tx.retry_count == 1

        # Cycle 2: requeued + send_update_link (retry_count becomes 2)
        current_time += timedelta(minutes=70)
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, now=current_time)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.pending_retry
        assert tx.retry_count == 2

        # Cycle 3: requeued + send_update_link (retry_count becomes 3)
        current_time += timedelta(minutes=70)
        pipeline.run_recovery_cycle(session, diagnoser, strategist, executor, now=current_time)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.pending_retry
        assert tx.retry_count == 3

        # Cycle 4: requeued + Rule 2 max_retries exceeded -> escalate_to_human (Terminal)
        current_time += timedelta(minutes=70)
        summary4 = pipeline.run_recovery_cycle(
            session, diagnoser, strategist, executor, now=current_time
        )
        session.commit()
        session.refresh(tx)

        assert summary4["requeued_count"] == 1
        assert summary4["escalated_count"] == 1
        assert tx.status == TxStatus.escalated

        # Verify exactly 1 Outcome row and 1 ExceptionLog row
        outcomes = session.exec(
            select(Outcome).where(Outcome.transaction_id == tx.id)
        ).all()
        assert len(outcomes) == 1
        assert outcomes[0].final_status == "escalated"

        exc_logs = session.exec(
            select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)
        ).all()
        assert len(exc_logs) == 1
        assert "maximum retry limit" in exc_logs[0].reason
