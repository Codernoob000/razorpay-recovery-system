"""
tests/test_database.py
=======================
Phase 2 verification: table creation, CRUD operations and relational
integrity for all 5 SQLModel tables, running against an in-memory SQLite
database so no .env or real DB is required.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

# ---------------------------------------------------------------------------
# Patch settings BEFORE importing anything from the package so that
# get_settings() never tries to read GEMINI_API_KEY from the environment.
# ---------------------------------------------------------------------------
FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-phase2",
    "DATABASE_URL": "sqlite:///./test_phase2.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.database import init_db
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine", scope="module")
def engine_fixture():
    """In-memory SQLite engine – created once per test module."""
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
    """Fresh session per test; rolls back after each test to keep isolation."""
    with Session(engine) as session:
        yield session
        session.rollback()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _make_transaction(**overrides) -> Transaction:
    defaults = dict(
        id=str(uuid.uuid4()),
        customer_id="cust_001",
        amount=999.00,
        type="subscription",
        status=TxStatus.failed,
        failure_code="insufficient_funds",
        retry_count=0,
        customer_value_tier=CustomerTier.enterprise,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    defaults.update(overrides)
    return Transaction(**defaults)


# ---------------------------------------------------------------------------
# Tests: Table creation
# ---------------------------------------------------------------------------


class TestTableCreation:
    def test_all_tables_exist(self, engine):
        """All 5 tables must be present in the metadata after init_db."""
        table_names = set(SQLModel.metadata.tables.keys())
        expected = {"transaction", "diagnosis", "recovery_action", "outcome", "exception_log"}
        assert expected.issubset(table_names), f"Missing tables: {expected - table_names}"

    def test_init_db_is_idempotent(self, engine):
        """Calling init_db twice must not raise (CREATE TABLE IF NOT EXISTS)."""
        init_db(custom_engine=engine)  # second call
        init_db(custom_engine=engine)  # third call – still fine


# ---------------------------------------------------------------------------
# Tests: Transaction CRUD
# ---------------------------------------------------------------------------


class TestTransactionCRUD:
    def test_create_transaction(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()
        session.refresh(tx)
        assert tx.id is not None

    def test_read_transaction(self, session):
        tx = _make_transaction(customer_id="cust_read")
        session.add(tx)
        session.commit()

        fetched = session.get(Transaction, tx.id)
        assert fetched is not None
        assert fetched.customer_id == "cust_read"

    def test_update_transaction_status(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        tx.status = TxStatus.pending_retry
        tx.retry_count = 1
        session.add(tx)
        session.commit()
        session.refresh(tx)

        assert tx.status == TxStatus.pending_retry
        assert tx.retry_count == 1

    def test_delete_transaction(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()
        tx_id = tx.id

        session.delete(tx)
        session.commit()

        assert session.get(Transaction, tx_id) is None

    def test_default_status_is_failed(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()
        session.refresh(tx)
        assert tx.status == TxStatus.failed

    def test_default_retry_count_is_zero(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()
        session.refresh(tx)
        assert tx.retry_count == 0

    def test_index_by_customer_id(self, session):
        cid = "cust_idx_" + str(uuid.uuid4())
        for _ in range(3):
            session.add(_make_transaction(customer_id=cid))
        session.commit()

        results = session.exec(select(Transaction).where(Transaction.customer_id == cid)).all()
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Tests: Diagnosis CRUD
# ---------------------------------------------------------------------------


class TestDiagnosisCRUD:
    def test_create_diagnosis(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.soft_decline,
            confidence=0.92,
            reasoning="Insufficient funds at end of billing cycle.",
            created_at=_utcnow(),
        )
        session.add(diag)
        session.commit()
        session.refresh(diag)

        assert diag.id is not None
        assert diag.classification == FailureCategory.soft_decline

    def test_read_diagnosis(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.technical_failure,
            confidence=0.85,
            reasoning="Gateway timeout.",
            created_at=_utcnow(),
        )
        session.add(diag)
        session.commit()

        fetched = session.get(Diagnosis, diag.id)
        assert fetched.confidence == pytest.approx(0.85)

    def test_multiple_diagnoses_per_transaction(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        for cat in [FailureCategory.soft_decline, FailureCategory.risk_hold]:
            session.add(Diagnosis(
                transaction_id=tx.id,
                classification=cat,
                confidence=0.7,
                reasoning="test",
                created_at=_utcnow(),
            ))
        session.commit()

        results = session.exec(
            select(Diagnosis).where(Diagnosis.transaction_id == tx.id)
        ).all()
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Tests: RecoveryAction CRUD
# ---------------------------------------------------------------------------


class TestRecoveryActionCRUD:
    def test_create_recovery_action(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        action = RecoveryAction(
            transaction_id=tx.id,
            action_type=ActionType.retry_payment,
            justification="Soft decline – high-value customer, retry after 60 min.",
            bounds_applied='{"max_retries": 3}',
            created_at=_utcnow(),
        )
        session.add(action)
        session.commit()
        session.refresh(action)

        assert action.id is not None
        assert action.action_type == ActionType.retry_payment

    def test_all_action_types_storable(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        for at in ActionType:
            session.add(RecoveryAction(
                transaction_id=tx.id,
                action_type=at,
                justification=f"Test {at.value}",
                bounds_applied="{}",
                created_at=_utcnow(),
            ))
        session.commit()

        results = session.exec(
            select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)
        ).all()
        assert len(results) == len(ActionType)


# ---------------------------------------------------------------------------
# Tests: Outcome CRUD
# ---------------------------------------------------------------------------


class TestOutcomeCRUD:
    def test_create_outcome(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        outcome = Outcome(
            transaction_id=tx.id,
            final_status="payment_succeeded",
            amount_recovered=999.00,
            resolved_at=_utcnow(),
        )
        session.add(outcome)
        session.commit()
        session.refresh(outcome)

        assert outcome.id is not None
        assert outcome.amount_recovered == pytest.approx(999.00)

    def test_default_amount_recovered_is_zero(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        outcome = Outcome(
            transaction_id=tx.id,
            final_status="abandoned",
            resolved_at=_utcnow(),
        )
        session.add(outcome)
        session.commit()
        session.refresh(outcome)

        assert outcome.amount_recovered == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests: ExceptionLog CRUD
# ---------------------------------------------------------------------------


class TestExceptionLogCRUD:
    def test_create_exception_log(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        log = ExceptionLog(
            transaction_id=tx.id,
            reason="Risk hold flagged by fraud engine.",
            escalated_at=_utcnow(),
        )
        session.add(log)
        session.commit()
        session.refresh(log)

        assert log.id is not None
        assert "Risk hold" in log.reason

    def test_multiple_logs_per_transaction(self, session):
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        for i in range(3):
            session.add(ExceptionLog(
                transaction_id=tx.id,
                reason=f"Escalation attempt {i}",
                escalated_at=_utcnow(),
            ))
        session.commit()

        results = session.exec(
            select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)
        ).all()
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Tests: Relational integrity (FK constraints via cascaded reads)
# ---------------------------------------------------------------------------


class TestRelationalIntegrity:
    def test_full_recovery_chain(self, session):
        """
        Create a complete recovery chain:
        Transaction → Diagnosis → RecoveryAction → Outcome → ExceptionLog
        and verify all records are retrievable via their foreign keys.
        """
        tx = _make_transaction(customer_id="cust_chain", amount=4999.00)
        session.add(tx)
        session.commit()

        diag = Diagnosis(
            transaction_id=tx.id,
            classification=FailureCategory.hard_decline,
            confidence=0.99,
            reasoning="Card permanently blocked.",
            created_at=_utcnow(),
        )
        action = RecoveryAction(
            transaction_id=tx.id,
            action_type=ActionType.escalate_to_human,
            justification="Hard decline – cannot auto-recover.",
            bounds_applied='{"risk_hold_action": "escalate_to_human"}',
            created_at=_utcnow(),
        )
        outcome = Outcome(
            transaction_id=tx.id,
            final_status="escalated",
            amount_recovered=0.0,
            resolved_at=_utcnow(),
        )
        exc_log = ExceptionLog(
            transaction_id=tx.id,
            reason="Hard decline; escalated to account manager.",
            escalated_at=_utcnow(),
        )

        session.add_all([diag, action, outcome, exc_log])
        session.commit()

        # Verify the entire chain resolves via FK
        assert session.exec(select(Diagnosis).where(Diagnosis.transaction_id == tx.id)).first() is not None
        assert session.exec(select(RecoveryAction).where(RecoveryAction.transaction_id == tx.id)).first() is not None
        assert session.exec(select(Outcome).where(Outcome.transaction_id == tx.id)).first() is not None
        assert session.exec(select(ExceptionLog).where(ExceptionLog.transaction_id == tx.id)).first() is not None

    def test_all_tx_statuses_storable(self, session):
        """Every TxStatus enum value must round-trip through the DB."""
        for status in TxStatus:
            tx = _make_transaction(status=status)
            session.add(tx)
        session.commit()

        for status in TxStatus:
            results = session.exec(
                select(Transaction).where(Transaction.status == status)
            ).all()
            assert len(results) >= 1

    def test_all_failure_categories_storable(self, session):
        """Every FailureCategory enum value must round-trip through the DB."""
        tx = _make_transaction()
        session.add(tx)
        session.commit()

        for cat in FailureCategory:
            session.add(Diagnosis(
                transaction_id=tx.id,
                classification=cat,
                confidence=0.5,
                reasoning=f"Test {cat.value}",
                created_at=_utcnow(),
            ))
        session.commit()

        results = session.exec(
            select(Diagnosis).where(Diagnosis.transaction_id == tx.id)
        ).all()
        assert len(results) == len(FailureCategory)

    def test_all_customer_tiers_storable(self, session):
        """Every CustomerTier enum value must round-trip through the DB."""
        for tier in CustomerTier:
            tx = _make_transaction(customer_value_tier=tier)
            session.add(tx)
        session.commit()

        for tier in CustomerTier:
            results = session.exec(
                select(Transaction).where(Transaction.customer_value_tier == tier)
            ).all()
            assert len(results) >= 1
