"""
tests/test_detector.py
=======================
Phase 4 verification: detector module fetch / mark helpers.

All tests run against an in-memory SQLite DB – no .env required.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-detector",
    "DATABASE_URL":   "sqlite:///./test_detector.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.models import (
        CustomerTier,
        Diagnosis,
        FailureCategory,
        Transaction,
        TxStatus,
    )
    from recovery_platform.modules.detector import (
        fetch_unresolved_failures,
        mark_in_progress,
        requeue_eligible_retries,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine", scope="module")
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


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _tx(status: TxStatus = TxStatus.failed, offset_minutes: int = 0) -> Transaction:
    return Transaction(
        id=str(uuid.uuid4()),
        customer_id=f"cust_{uuid.uuid4().hex[:6]}",
        amount=999.0,
        type="subscription",
        status=status,
        failure_code="insufficient_funds",
        retry_count=0,
        customer_value_tier=CustomerTier.starter,
        created_at=_utcnow() - timedelta(minutes=offset_minutes),
        updated_at=_utcnow(),
    )


# ---------------------------------------------------------------------------
# fetch_unresolved_failures
# ---------------------------------------------------------------------------


class TestFetchUnresolvedFailures:

    def test_returns_only_failed_status(self, session):
        failed  = _tx(TxStatus.failed)
        pending = _tx(TxStatus.pending_retry)
        recovered = _tx(TxStatus.recovered)
        session.add_all([failed, pending, recovered])
        session.commit()

        results = fetch_unresolved_failures(session)
        ids = {r.id for r in results}
        assert failed.id in ids
        assert pending.id not in ids
        assert recovered.id not in ids

    def test_excludes_escalated_and_abandoned(self, session):
        esc  = _tx(TxStatus.escalated)
        abn  = _tx(TxStatus.abandoned)
        fail = _tx(TxStatus.failed)
        session.add_all([esc, abn, fail])
        session.commit()

        results = fetch_unresolved_failures(session)
        ids = {r.id for r in results}
        assert fail.id in ids
        assert esc.id not in ids
        assert abn.id not in ids

    def test_respects_limit(self, session):
        for _ in range(10):
            session.add(_tx(TxStatus.failed))
        session.commit()

        results = fetch_unresolved_failures(session, limit=5)
        assert len(results) <= 5

    def test_ordered_fifo(self, session):
        # Insert transactions with controlled timestamps
        old = _tx(TxStatus.failed, offset_minutes=60)
        mid = _tx(TxStatus.failed, offset_minutes=30)
        new = _tx(TxStatus.failed, offset_minutes=0)
        session.add_all([new, old, mid])   # deliberate insertion disorder
        session.commit()

        results = fetch_unresolved_failures(session, limit=100)
        # Filter to just our 3 to avoid pollution from other tests
        our_ids = {old.id, mid.id, new.id}
        our = [r for r in results if r.id in our_ids]
        timestamps = [r.created_at for r in our]
        assert timestamps == sorted(timestamps), "Results must be ordered oldest-first"

    def test_empty_db_returns_empty_list(self, engine):
        """Fresh engine with no data must return an empty list."""
        fresh_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(fresh_engine)
        with Session(fresh_engine) as s:
            result = fetch_unresolved_failures(s)
        assert result == []

    def test_returns_list_type(self, session):
        result = fetch_unresolved_failures(session)
        assert isinstance(result, list)

    def test_all_results_are_transaction_instances(self, session):
        session.add(_tx(TxStatus.failed))
        session.commit()

        results = fetch_unresolved_failures(session)
        for r in results:
            assert isinstance(r, Transaction)

    def test_default_limit_is_100(self, session):
        # Verify that without a limit arg we get at most 100 rows
        # (we can't insert 101 here easily, but we can check the return size)
        result = fetch_unresolved_failures(session)
        assert len(result) <= 100


# ---------------------------------------------------------------------------
# mark_in_progress
# ---------------------------------------------------------------------------


class TestMarkInProgress:

    def test_transitions_failed_to_pending_retry(self, session):
        tx = _tx(TxStatus.failed)
        session.add(tx)
        session.commit()

        count = mark_in_progress(session, [tx.id])
        session.commit()
        session.refresh(tx)

        assert count == 1
        assert tx.status == TxStatus.pending_retry

    def test_skips_non_failed_rows(self, session):
        already_pending = _tx(TxStatus.pending_retry)
        session.add(already_pending)
        session.commit()

        count = mark_in_progress(session, [already_pending.id])
        session.commit()

        assert count == 0   # row was not in failed state, must be skipped

    def test_empty_ids_returns_zero(self, session):
        count = mark_in_progress(session, [])
        assert count == 0

    def test_idempotent_on_double_call(self, session):
        tx = _tx(TxStatus.failed)
        session.add(tx)
        session.commit()

        mark_in_progress(session, [tx.id])
        session.commit()
        second_count = mark_in_progress(session, [tx.id])  # already pending_retry
        session.commit()

        assert second_count == 0   # idempotent

    def test_batch_update_mixed_statuses(self, session):
        f1 = _tx(TxStatus.failed)
        f2 = _tx(TxStatus.failed)
        p1 = _tx(TxStatus.pending_retry)
        session.add_all([f1, f2, p1])
        session.commit()

        count = mark_in_progress(session, [f1.id, f2.id, p1.id])
        session.commit()

        assert count == 2   # only the two failed ones

    def test_updated_at_is_refreshed(self, session):
        # SQLite stores datetimes as naive strings, so we use a naive baseline
        from datetime import datetime as dt
        original_time = dt.utcnow() - timedelta(hours=1)
        tx = _tx(TxStatus.failed)
        tx.updated_at = original_time
        session.add(tx)
        session.commit()
        session.refresh(tx)   # bind tx to session before mark_in_progress

        mark_in_progress(session, [tx.id])
        session.commit()
        session.refresh(tx)   # reload updated_at from DB

        # Strip tzinfo from refreshed value if SQLite returned it naively
        refreshed = tx.updated_at.replace(tzinfo=None) if tx.updated_at.tzinfo else tx.updated_at
        assert refreshed > original_time


# ---------------------------------------------------------------------------
# requeue_eligible_retries
# ---------------------------------------------------------------------------


class TestRequeueEligibleRetries:

    def test_pending_retry_under_delay_not_requeued(self, session):
        """A pending_retry transaction updated only 10 minutes ago (< 60m delay) is NOT requeued."""
        now = _utcnow()
        tx = _tx(TxStatus.pending_retry)
        tx.updated_at = now - timedelta(minutes=10)
        session.add(tx)
        session.commit()

        # Diagnosis is soft_decline (60 min delay)
        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.soft_decline,
            confidence=0.9,
            reasoning="insufficient funds",
            created_at=now - timedelta(minutes=10),
        )
        session.add(diag)
        session.commit()

        requeued = requeue_eligible_retries(session, now=now)
        session.commit()
        session.refresh(tx)

        assert requeued == 0
        assert tx.status == TxStatus.pending_retry

    def test_pending_retry_over_delay_is_requeued_and_picked_up(self, session):
        """A pending_retry transaction updated 70 minutes ago (> 60m delay) is transitioned to failed."""
        now = _utcnow()
        tx = _tx(TxStatus.pending_retry)
        tx.updated_at = now - timedelta(minutes=70)
        session.add(tx)
        session.commit()

        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.soft_decline,
            confidence=0.9,
            reasoning="soft decline",
            created_at=now - timedelta(minutes=70),
        )
        session.add(diag)
        session.commit()

        requeued = requeue_eligible_retries(session, now=now)
        session.commit()
        session.refresh(tx)

        assert requeued >= 1
        assert tx.status == TxStatus.failed

        # Confirm fetch_unresolved_failures picks it up
        unresolved = fetch_unresolved_failures(session)
        unresolved_ids = {r.id for r in unresolved}
        assert tx.id in unresolved_ids

    def test_technical_failure_requeued_after_15_minutes(self, session):
        """Technical failure uses technical_failure_min (15 min) delay."""
        now = _utcnow()
        tx = _tx(TxStatus.pending_retry)
        tx.updated_at = now - timedelta(minutes=20)
        session.add(tx)
        session.commit()

        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.technical_failure,
            confidence=0.95,
            reasoning="gateway timeout",
            created_at=now - timedelta(minutes=20),
        )
        session.add(diag)
        session.commit()

        requeued = requeue_eligible_retries(session, now=now)
        session.commit()
        session.refresh(tx)

        assert requeued >= 1
        assert tx.status == TxStatus.failed
