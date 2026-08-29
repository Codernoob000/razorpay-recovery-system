"""
recovery_platform/modules/strategist.py
=========================================
Deterministic, policy-bounded recovery decision engine.

Rules are evaluated in strict priority order so higher-priority gates
can never be bypassed by lower-priority logic.

Rule constants
--------------
RULE_RISK_HOLD_ZERO_RETRY         – Any risk_hold classification forces escalation.
RULE_MAX_RETRIES_EXCEEDED         – retry_count >= policy.max_retries forces escalation.
RULE_HARD_DECLINE_UPDATE_LINK     – Hard declines route to payment-method update.
RULE_DISCOUNT_ELIGIBLE_ENTERPRISE – Enterprise + 1+ retries gets discount offer.
RULE_DISCOUNT_ALREADY_OFFERED     – Discount already issued; fall through to retry.
RULE_RETRY_ALLOWED                – Default soft-decline action.
RULE_TECHNICAL_RETRY_ALLOWED      – Default technical-failure action.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from recovery_platform.models import (
    ActionType,
    FailureCategory,
    RecoveryAction,
    Transaction,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule-ID constants (used as bound labels and in test assertions)
# ---------------------------------------------------------------------------

RULE_RISK_HOLD_ZERO_RETRY         = "RULE_RISK_HOLD_ZERO_RETRY"
RULE_MAX_RETRIES_EXCEEDED         = "RULE_MAX_RETRIES_EXCEEDED"
RULE_HARD_DECLINE_UPDATE_LINK     = "RULE_HARD_DECLINE_UPDATE_LINK"
RULE_DISCOUNT_ELIGIBLE_ENTERPRISE = "RULE_DISCOUNT_ELIGIBLE_ENTERPRISE"
RULE_DISCOUNT_ALREADY_OFFERED     = "RULE_DISCOUNT_ALREADY_OFFERED"
RULE_RETRY_ALLOWED                = "RULE_RETRY_ALLOWED"
RULE_TECHNICAL_RETRY_ALLOWED      = "RULE_TECHNICAL_RETRY_ALLOWED"


# ---------------------------------------------------------------------------
# Strategist
# ---------------------------------------------------------------------------


class Strategist:
    """
    Applies deterministic policy bounds to select a recovery action.

    Parameters
    ----------
    policy:
        ``RecoveryPolicy`` from ``get_settings().recovery_policy``.
        If *None*, loaded from settings at construction time.
    """

    def __init__(self, policy=None) -> None:
        if policy is None:
            from recovery_platform.config import get_settings
            policy = get_settings().recovery_policy
        self._policy = policy

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def evaluate_strategy(
        self,
        tx: Transaction,
        diagnosis,
        session: Session | None = None,
    ) -> tuple[ActionType, str, list[str]]:
        """
        Select the recovery action for *tx* given its *diagnosis*.

        Rules are evaluated in strict priority order (1 → 6).  The first
        matching rule wins; subsequent rules are not evaluated.

        Parameters
        ----------
        tx:
            The failed transaction being evaluated.
        diagnosis:
            A ``DiagnosisOutput`` or any object with a ``.classification``
            attribute of type ``FailureCategory``.

        Returns
        -------
        tuple[ActionType, str, list[str]]
            ``(action_type, justification, bounds_applied)``
        """
        classification = diagnosis.classification
        retry_count    = tx.retry_count
        tier           = tx.customer_value_tier

        # ── Rule 1: Risk Hold gating ──────────────────────────────────
        if classification == FailureCategory.risk_hold:
            return (
                ActionType.escalate_to_human,
                (
                    f"Transaction {tx.id} classified as risk_hold "
                    f"(confidence={getattr(diagnosis, 'confidence', '?'):.2f}). "
                    f"Policy mandates immediate human escalation for all risk signals."
                ),
                [RULE_RISK_HOLD_ZERO_RETRY],
            )

        # ── Rule 2: Max retries exhausted ─────────────────────────────
        if retry_count >= self._policy.max_retries:
            return (
                ActionType.escalate_to_human,
                (
                    f"Transaction {tx.id} has reached the maximum retry limit "
                    f"({retry_count}/{self._policy.max_retries}). "
                    f"Escalating to human agent for manual resolution."
                ),
                [RULE_MAX_RETRIES_EXCEEDED],
            )

        # ── Rule 3: Hard decline ──────────────────────────────────────
        if classification == FailureCategory.hard_decline:
            return (
                ActionType.send_update_link,
                (
                    f"Transaction {tx.id} is a hard decline ({tx.failure_code}). "
                    f"Sending payment-method update link to customer."
                ),
                [RULE_HARD_DECLINE_UPDATE_LINK],
            )

        # ── Rule 4: Soft decline ──────────────────────────────────
        if classification == FailureCategory.soft_decline:
            eligible_tier = self._policy.discount_eligibility.min_tier
            if tier.value == eligible_tier and retry_count >= 1:
                # Guard: only offer discount if one hasn't already been issued
                # for this specific transaction (check RecoveryAction history).
                if session is not None and self._discount_already_offered(session, tx.id):
                    logger.info(
                        "Discount already issued for tx=%s – falling back to retry.",
                        tx.id,
                    )
                    return (
                        ActionType.retry_payment,
                        (
                            f"Transaction {tx.id}: discount was already offered in a "
                            f"prior cycle. Scheduling a plain retry instead."
                        ),
                        [RULE_DISCOUNT_ALREADY_OFFERED],
                    )
                max_pct = self._policy.discount_eligibility.max_pct
                return (
                    ActionType.offer_discount,
                    (
                        f"Transaction {tx.id}: {tier.value} customer with "
                        f"{retry_count} prior attempt(s). "
                        f"Offering discount up to {max_pct}% to recover revenue."
                    ),
                    [RULE_DISCOUNT_ELIGIBLE_ENTERPRISE],
                )
            return (
                ActionType.retry_payment,
                (
                    f"Transaction {tx.id}: soft decline ({tx.failure_code}), "
                    f"retry_count={retry_count}. Scheduling automatic retry."
                ),
                [RULE_RETRY_ALLOWED],
            )

        # ── Rule 5: Technical failure ─────────────────────────────────
        if classification == FailureCategory.technical_failure:
            return (
                ActionType.retry_payment,
                (
                    f"Transaction {tx.id}: technical failure ({tx.failure_code}). "
                    f"Retrying after gateway stabilisation window."
                ),
                [RULE_TECHNICAL_RETRY_ALLOWED],
            )

        # ── Fallback (should never be reached with valid enums) ───────
        logger.warning(
            "Strategist: unexpected classification %r for tx=%s – defaulting to escalate",
            classification, tx.id,
        )
        return (
            ActionType.escalate_to_human,
            f"Unrecognised classification {classification!r} for tx {tx.id}.",
            ["RULE_UNKNOWN_FALLBACK"],
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _discount_already_offered(session: Session, transaction_id: str) -> bool:
        """
        Return True if any ``RecoveryAction`` for *transaction_id* already has
        ``action_type == ActionType.offer_discount``.

        Performs an explicit ``select`` because no ORM relationship exists
        between ``Transaction`` and ``RecoveryAction``.
        """
        stmt = (
            select(RecoveryAction)
            .where(RecoveryAction.transaction_id == transaction_id)
            .where(RecoveryAction.action_type == ActionType.offer_discount)
            .limit(1)
        )
        result = session.exec(stmt).first()
        return result is not None
