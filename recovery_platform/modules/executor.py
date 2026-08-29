"""
recovery_platform/modules/executor.py
========================================
Simulates payment recovery action execution and persists state transitions.

The ``Executor`` does NOT call external payment gateways – it models
real-world outcomes probabilistically so the pipeline can be tested and
demonstrated without live credentials.  Replace the simulation layer with
real Razorpay / Stripe calls when deploying to production.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime

from sqlmodel import Session

from recovery_platform.models import (
    ActionType,
    ExceptionLog,
    Outcome,
    RecoveryAction,
    Transaction,
    TxStatus,
)

logger = logging.getLogger(__name__)


class Executor:
    """
    Applies an ``ActionType`` to a ``Transaction`` and persists all side-effects.

    Parameters
    ----------
    policy:
        ``RecoveryPolicy`` from settings.  If *None*, loaded from settings.
    seed:
        Optional random seed for deterministic simulation in tests.
    """

    # Simulation success probabilities
    _SOFT_DECLINE_RETRY_SUCCESS_PROB  = 0.70
    _TECHNICAL_RETRY_SUCCESS_PROB     = 0.85

    def __init__(self, policy=None, seed: int | None = None) -> None:
        if policy is None:
            from recovery_platform.config import get_settings
            policy = get_settings().recovery_policy
        self._policy = policy
        self._rng    = random.Random(seed)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def execute_action(
        self,
        session: Session,
        tx: Transaction,
        action_type: ActionType,
        bounds_applied: list[str],
        justification: str,
    ) -> tuple[TxStatus, float]:
        """
        Execute *action_type* on *tx* and write ``RecoveryAction``, ``Outcome``
        (and optionally ``ExceptionLog``) rows to the database.

        Parameters
        ----------
        session:
            Active SQLModel session (caller commits).
        tx:
            ``Transaction`` to act upon.
        action_type:
            The action chosen by the Strategist.
        bounds_applied:
            List of rule-ID strings that were applied.
        justification:
            Human-readable explanation string.

        Returns
        -------
        tuple[TxStatus, float]
            ``(new_status, amount_recovered)``
        """
        now = datetime.now(tz=UTC)

        # ── Dispatch ──────────────────────────────────────────────────
        if action_type == ActionType.retry_payment:
            new_status, amount_recovered = self._handle_retry(
                session, tx, justification, now
            )
        elif action_type == ActionType.send_update_link:
            new_status, amount_recovered = self._handle_update_link(tx)
        elif action_type == ActionType.offer_discount:
            new_status, amount_recovered = self._handle_discount(tx)
        elif action_type == ActionType.escalate_to_human:
            new_status, amount_recovered = self._handle_escalation(
                session, tx, justification, now
            )
        else:
            # terminate_subscription or unknown
            new_status       = TxStatus.abandoned
            amount_recovered = 0.0

        # ── Update transaction ─────────────────────────────────────────
        tx.status     = new_status
        tx.updated_at = now
        session.add(tx)

        # ── Persist RecoveryAction ────────────────────────────────────
        action_row = RecoveryAction(
            transaction_id=tx.id,
            action_type=action_type,
            justification=justification,
            bounds_applied=json.dumps(bounds_applied),
            created_at=now,
        )
        session.add(action_row)

        # ── Persist Outcome (terminal states only) ─────────────────────
        if new_status in (TxStatus.recovered, TxStatus.escalated, TxStatus.abandoned):
            outcome_row = Outcome(
                transaction_id=tx.id,
                final_status=new_status.value,
                amount_recovered=amount_recovered,
                resolved_at=now,
            )
            session.add(outcome_row)

        logger.info(
            "Executed %s on tx=%s -> status=%s  recovered=%.2f",
            action_type.value, tx.id, new_status.value, amount_recovered,
        )
        return new_status, amount_recovered

    # ------------------------------------------------------------------
    # Private handlers
    # ------------------------------------------------------------------

    def _handle_retry(
        self,
        session: Session,
        tx: Transaction,
        justification: str,
        now: datetime,
    ) -> tuple[TxStatus, float]:
        tx.retry_count += 1

        # Choose success probability based on failure context
        # (We infer context from retry_count; caller has full diagnosis if needed)
        success_prob = (
            self._SOFT_DECLINE_RETRY_SUCCESS_PROB
            if tx.retry_count <= 2
            else self._TECHNICAL_RETRY_SUCCESS_PROB
        )

        if self._rng.random() < success_prob:
            return TxStatus.recovered, tx.amount

        # Retry failed – check if we've now hit the limit
        if tx.retry_count >= self._policy.max_retries:
            exc_log = ExceptionLog(
                transaction_id=tx.id,
                reason=f"Max retries ({self._policy.max_retries}) exhausted. Last failure: {tx.failure_code}",
                escalated_at=now,
            )
            session.add(exc_log)
            return TxStatus.escalated, 0.0
        return TxStatus.pending_retry, 0.0

    def _handle_update_link(
        self, tx: Transaction
    ) -> tuple[TxStatus, float]:
        tx.retry_count += 1
        return TxStatus.pending_retry, 0.0

    def _handle_discount(
        self, tx: Transaction
    ) -> tuple[TxStatus, float]:
        discount_pct    = self._policy.discount_eligibility.max_pct / 100.0
        amount_recovered = tx.amount * (1.0 - discount_pct)
        return TxStatus.recovered, round(amount_recovered, 2)

    def _handle_escalation(
        self,
        session: Session,
        tx: Transaction,
        justification: str,
        now: datetime,
    ) -> tuple[TxStatus, float]:
        exc_log = ExceptionLog(
            transaction_id=tx.id,
            reason=justification,
            escalated_at=now,
        )
        session.add(exc_log)
        return TxStatus.escalated, 0.0
