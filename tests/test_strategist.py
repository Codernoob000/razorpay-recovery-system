"""
tests/test_strategist.py
=========================
Phase 5 – Strategist deterministic rule tests.
All mocked; no DB or real LLM required.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-strat",
    "DATABASE_URL":   "sqlite:///./test_strat.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.models import (
        ActionType,
        CustomerTier,
        FailureCategory,
        Transaction,
        TxStatus,
    )
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _policy(max_retries: int = 3, min_tier: str = "enterprise", max_pct: int = 15):
    """Build a minimal policy namespace accepted by Strategist."""
    return SimpleNamespace(
        max_retries=max_retries,
        discount_eligibility=SimpleNamespace(min_tier=min_tier, max_pct=max_pct),
    )


def _tx(
    retry_count: int = 0,
    tier: CustomerTier = CustomerTier.starter,
    failure_code: str = "insufficient_funds",
) -> Transaction:
    return Transaction(
        id=str(uuid.uuid4()),
        customer_id="cust_x",
        amount=5000.0,
        type="subscription",
        status=TxStatus.failed,
        failure_code=failure_code,
        retry_count=retry_count,
        customer_value_tier=tier,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )


def _diag(classification: FailureCategory, confidence: float = 0.9):
    return SimpleNamespace(classification=classification, confidence=confidence)


# ---------------------------------------------------------------------------
# Rule 1: Risk hold gating
# ---------------------------------------------------------------------------


class TestRiskHoldRule:

    def test_risk_hold_always_escalates(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=0), _diag(FailureCategory.risk_hold)
        )
        assert action == ActionType.escalate_to_human
        assert RULE_RISK_HOLD_ZERO_RETRY in bounds

    def test_risk_hold_overrides_zero_retries(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=0, tier=CustomerTier.enterprise),
            _diag(FailureCategory.risk_hold),
        )
        assert action == ActionType.escalate_to_human
        assert RULE_RISK_HOLD_ZERO_RETRY in bounds

    def test_risk_hold_overrides_enterprise_tier(self):
        """Even enterprise with 0 retries escalates if risk_hold."""
        strat = Strategist(policy=_policy())
        action, just, bounds = strat.evaluate_strategy(
            _tx(retry_count=0, tier=CustomerTier.enterprise),
            _diag(FailureCategory.risk_hold),
        )
        assert action == ActionType.escalate_to_human
        assert RULE_RISK_HOLD_ZERO_RETRY in bounds
        assert "risk_hold" in just.lower() or "human" in just.lower()

    def test_risk_hold_justification_not_empty(self):
        strat = Strategist(policy=_policy())
        _, just, _ = strat.evaluate_strategy(
            _tx(), _diag(FailureCategory.risk_hold)
        )
        assert len(just) > 10

    def test_risk_hold_all_retry_counts_escalate(self):
        """No matter how many retries, risk_hold always escalates (checked first)."""
        strat = Strategist(policy=_policy(max_retries=3))
        for n in [0, 1, 2, 3, 10]:
            action, _, bounds = strat.evaluate_strategy(
                _tx(retry_count=n), _diag(FailureCategory.risk_hold)
            )
            assert action == ActionType.escalate_to_human, f"Failed at retry_count={n}"
            assert RULE_RISK_HOLD_ZERO_RETRY in bounds


# ---------------------------------------------------------------------------
# Rule 2: Max retries exceeded
# ---------------------------------------------------------------------------


class TestMaxRetriesRule:

    def test_max_retries_reached_escalates(self):
        strat = Strategist(policy=_policy(max_retries=3))
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=3), _diag(FailureCategory.soft_decline)
        )
        assert action == ActionType.escalate_to_human
        assert RULE_MAX_RETRIES_EXCEEDED in bounds

    def test_above_max_retries_escalates(self):
        strat = Strategist(policy=_policy(max_retries=3))
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=5), _diag(FailureCategory.technical_failure)
        )
        assert action == ActionType.escalate_to_human
        assert RULE_MAX_RETRIES_EXCEEDED in bounds

    def test_below_max_retries_does_not_escalate_via_this_rule(self):
        strat = Strategist(policy=_policy(max_retries=3))
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=2), _diag(FailureCategory.soft_decline)
        )
        # Should NOT be escalated via RULE_MAX_RETRIES_EXCEEDED
        assert RULE_MAX_RETRIES_EXCEEDED not in bounds

    def test_max_retries_overrides_soft_decline_discount(self):
        """Enterprise with retry_count==3 hits limit before discount logic."""
        strat = Strategist(policy=_policy(max_retries=3))
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=3, tier=CustomerTier.enterprise),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.escalate_to_human
        assert RULE_MAX_RETRIES_EXCEEDED in bounds
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE not in bounds

    def test_justification_contains_retry_info(self):
        strat = Strategist(policy=_policy(max_retries=3))
        _, just, _ = strat.evaluate_strategy(
            _tx(retry_count=3), _diag(FailureCategory.soft_decline)
        )
        assert "3" in just  # retry counts visible in justification


# ---------------------------------------------------------------------------
# Rule 3: Hard decline
# ---------------------------------------------------------------------------


class TestHardDeclineRule:

    def test_hard_decline_sends_update_link(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(), _diag(FailureCategory.hard_decline)
        )
        assert action == ActionType.send_update_link
        assert RULE_HARD_DECLINE_UPDATE_LINK in bounds

    def test_hard_decline_all_tiers(self):
        strat = Strategist(policy=_policy())
        for tier in CustomerTier:
            action, _, bounds = strat.evaluate_strategy(
                _tx(tier=tier), _diag(FailureCategory.hard_decline)
            )
            assert action == ActionType.send_update_link, f"Failed for tier={tier}"

    def test_hard_decline_zero_retries(self):
        strat = Strategist(policy=_policy())
        action, _, _ = strat.evaluate_strategy(
            _tx(retry_count=0), _diag(FailureCategory.hard_decline)
        )
        assert action == ActionType.send_update_link

    def test_hard_decline_justification_not_empty(self):
        strat = Strategist(policy=_policy())
        _, just, _ = strat.evaluate_strategy(
            _tx(), _diag(FailureCategory.hard_decline)
        )
        assert len(just) > 10


# ---------------------------------------------------------------------------
# Rule 4: Soft decline
# ---------------------------------------------------------------------------


class TestSoftDeclineRule:

    def test_starter_tier_retries(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=0, tier=CustomerTier.starter),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.retry_payment
        assert RULE_RETRY_ALLOWED in bounds

    def test_business_tier_retries(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=1, tier=CustomerTier.business),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.retry_payment
        assert RULE_RETRY_ALLOWED in bounds

    def test_enterprise_first_attempt_retries(self):
        """Enterprise with retry_count=0 has not yet qualified for discount."""
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=0, tier=CustomerTier.enterprise),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.retry_payment
        assert RULE_RETRY_ALLOWED in bounds

    def test_enterprise_second_attempt_gets_discount(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=1, tier=CustomerTier.enterprise),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.offer_discount
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE in bounds

    def test_enterprise_third_attempt_gets_discount(self):
        strat = Strategist(policy=_policy(max_retries=5))
        action, _, bounds = strat.evaluate_strategy(
            _tx(retry_count=2, tier=CustomerTier.enterprise),
            _diag(FailureCategory.soft_decline),
        )
        assert action == ActionType.offer_discount

    def test_discount_justification_contains_max_pct(self):
        strat = Strategist(policy=_policy(max_pct=15))
        _, just, _ = strat.evaluate_strategy(
            _tx(retry_count=1, tier=CustomerTier.enterprise),
            _diag(FailureCategory.soft_decline),
        )
        assert "15" in just


# ---------------------------------------------------------------------------
# Rule 5: Technical failure
# ---------------------------------------------------------------------------


class TestTechnicalFailureRule:

    def test_technical_failure_retries(self):
        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            _tx(), _diag(FailureCategory.technical_failure)
        )
        assert action == ActionType.retry_payment
        assert RULE_TECHNICAL_RETRY_ALLOWED in bounds

    def test_technical_failure_all_non_exhausted_retries(self):
        strat = Strategist(policy=_policy(max_retries=3))
        for n in [0, 1, 2]:
            action, _, bounds = strat.evaluate_strategy(
                _tx(retry_count=n), _diag(FailureCategory.technical_failure)
            )
            assert action == ActionType.retry_payment, f"Failed at retry_count={n}"

    def test_technical_failure_justification_not_empty(self):
        strat = Strategist(policy=_policy())
        _, just, _ = strat.evaluate_strategy(
            _tx(), _diag(FailureCategory.technical_failure)
        )
        assert len(just) > 10


# ---------------------------------------------------------------------------
# Return shape contract
# ---------------------------------------------------------------------------


class TestReturnShape:

    @pytest.mark.parametrize("cat", list(FailureCategory))
    def test_always_returns_three_tuple(self, cat):
        strat = Strategist(policy=_policy())
        result = strat.evaluate_strategy(_tx(), _diag(cat))
        assert len(result) == 3
        action, just, bounds = result
        assert isinstance(action, ActionType)
        assert isinstance(just, str) and just
        assert isinstance(bounds, list) and bounds

    @pytest.mark.parametrize("cat", list(FailureCategory))
    def test_bounds_never_empty(self, cat):
        strat = Strategist(policy=_policy())
        _, _, bounds = strat.evaluate_strategy(_tx(), _diag(cat))
        assert len(bounds) >= 1


# ---------------------------------------------------------------------------
# RULE_DISCOUNT_ALREADY_OFFERED guard
# ---------------------------------------------------------------------------


class TestDiscountAlreadyOfferedGuard:
    """
    Verifies that an enterprise transaction which already has a prior
    offer_discount RecoveryAction does NOT receive a second discount.
    All tests in this class supply a real in-memory SQLite session so
    the DB-history check inside evaluate_strategy is exercised.
    """

    # --- fixtures / helpers -----------------------------------------------

    @pytest.fixture(name="db_session", scope="class")
    def db_session_fixture(self):
        from sqlmodel import Session, SQLModel, create_engine
        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            yield s

    def _enterprise_tx(self, retry_count: int = 1) -> Transaction:
        return _tx(retry_count=retry_count, tier=CustomerTier.enterprise)

    def _insert_tx(self, session, tx: Transaction) -> None:
        session.add(tx)
        session.commit()
        session.refresh(tx)

    def _insert_discount_action(self, session, transaction_id: str) -> None:
        import json as _json

        from recovery_platform.models import RecoveryAction
        row = RecoveryAction(
            transaction_id=transaction_id,
            action_type=ActionType.offer_discount,
            justification="Prior discount offer",
            bounds_applied=_json.dumps([RULE_DISCOUNT_ELIGIBLE_ENTERPRISE]),
            created_at=_utcnow(),
        )
        session.add(row)
        session.commit()

    # --- tests ------------------------------------------------------------

    def test_no_prior_discount_offers_discount(self, db_session):
        """Without any prior RecoveryAction, discount is offered normally."""
        tx = self._enterprise_tx(retry_count=1)
        self._insert_tx(db_session, tx)

        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            tx, _diag(FailureCategory.soft_decline), session=db_session
        )
        assert action == ActionType.offer_discount
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE in bounds
        assert RULE_DISCOUNT_ALREADY_OFFERED not in bounds

    def test_prior_discount_falls_back_to_retry(self, db_session):
        """Core invariant: second evaluation returns retry_payment, not offer_discount."""
        tx = self._enterprise_tx(retry_count=2)
        self._insert_tx(db_session, tx)
        self._insert_discount_action(db_session, tx.id)

        strat = Strategist(policy=_policy())
        action, _, bounds = strat.evaluate_strategy(
            tx, _diag(FailureCategory.soft_decline), session=db_session
        )
        assert action == ActionType.retry_payment
        assert RULE_DISCOUNT_ALREADY_OFFERED in bounds
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE not in bounds

    def test_prior_discount_justification_mentions_prior_cycle(self, db_session):
        """Justification string must indicate a prior discount was already issued."""
        tx = self._enterprise_tx(retry_count=2)
        self._insert_tx(db_session, tx)
        self._insert_discount_action(db_session, tx.id)

        strat = Strategist(policy=_policy())
        _, just, _ = strat.evaluate_strategy(
            tx, _diag(FailureCategory.soft_decline), session=db_session
        )
        assert "already" in just.lower() or "prior" in just.lower()

    def test_no_session_still_offers_discount(self):
        """When session=None the guard is skipped; existing behaviour preserved."""
        tx = self._enterprise_tx(retry_count=1)
        strat = Strategist(policy=_policy())
        # No session passed – guard cannot run; discount should be offered.
        action, _, bounds = strat.evaluate_strategy(
            tx, _diag(FailureCategory.soft_decline)
            # session defaults to None
        )
        assert action == ActionType.offer_discount
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE in bounds

    def test_non_enterprise_unaffected_by_guard(self, db_session):
        """The guard only applies to the discount-eligible path; other tiers are unaffected."""
        for tier in (CustomerTier.starter, CustomerTier.business):
            tx = _tx(retry_count=2, tier=tier)
            self._insert_tx(db_session, tx)
            # Insert a discount action anyway (should not matter for non-enterprise)
            self._insert_discount_action(db_session, tx.id)

            strat = Strategist(policy=_policy())
            action, _, bounds = strat.evaluate_strategy(
                tx, _diag(FailureCategory.soft_decline), session=db_session
            )
            # Non-enterprise tiers are never eligible for discount – plain retry
            assert action == ActionType.retry_payment
            assert RULE_RETRY_ALLOWED in bounds
            assert RULE_DISCOUNT_ALREADY_OFFERED not in bounds

    def test_guard_does_not_affect_non_soft_decline(self, db_session):
        """The discount guard must not interfere with hard_decline or technical_failure."""
        tx = self._enterprise_tx(retry_count=1)
        self._insert_tx(db_session, tx)
        self._insert_discount_action(db_session, tx.id)

        strat = Strategist(policy=_policy())
        # Hard decline -> send_update_link (guard not in this path)
        action_h, _, bounds_h = strat.evaluate_strategy(
            tx, _diag(FailureCategory.hard_decline), session=db_session
        )
        assert action_h == ActionType.send_update_link
        assert RULE_DISCOUNT_ALREADY_OFFERED not in bounds_h

        # Technical failure -> retry_payment (guard not in this path)
        action_t, _, bounds_t = strat.evaluate_strategy(
            tx, _diag(FailureCategory.technical_failure), session=db_session
        )
        assert action_t == ActionType.retry_payment
        assert RULE_DISCOUNT_ALREADY_OFFERED not in bounds_t

    def test_enterprise_tx_with_prior_discount_receives_retry_action_and_not_second_discount(self, db_session):
        """
        An enterprise transaction that already has one offer_discount RecoveryAction
        on record does NOT receive a second one on the next Strategist evaluation,
        and instead receives retry_payment with RULE_DISCOUNT_ALREADY_OFFERED bound.
        """
        tx = self._enterprise_tx(retry_count=1)
        self._insert_tx(db_session, tx)
        self._insert_discount_action(db_session, tx.id)

        strat = Strategist(policy=_policy())
        action, justification, bounds = strat.evaluate_strategy(
            tx, _diag(FailureCategory.soft_decline), session=db_session
        )

        assert action == ActionType.retry_payment
        assert action != ActionType.offer_discount
        assert bounds == [RULE_DISCOUNT_ALREADY_OFFERED]
        assert RULE_DISCOUNT_ELIGIBLE_ENTERPRISE not in bounds
        assert "discount was already offered" in justification.lower() or "prior" in justification.lower()
