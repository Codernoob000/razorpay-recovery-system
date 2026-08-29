"""
recovery_platform/models.py
============================
Normalized relational schema for the AI Revenue Recovery Platform.

Tables
------
Transaction     – Root payment record (one per failed charge).
Diagnosis       – Gemini classification result for a transaction.
RecoveryAction  – Action chosen by the AI agent for recovery.
Outcome         – Final resolution of a recovery attempt.
ExceptionLog    – Audit trail for escalations / unrecoverable failures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return a timezone-aware UTC datetime (Python 3.11+ compatible)."""
    return datetime.now(tz=UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class FailureCategory(str, Enum):
    """Root cause classification for a payment failure."""

    soft_decline = "soft_decline"
    hard_decline = "hard_decline"
    technical_failure = "technical_failure"
    risk_hold = "risk_hold"


class ActionType(str, Enum):
    """Discrete recovery actions the AI agent may recommend."""

    retry_payment = "retry_payment"
    send_update_link = "send_update_link"
    offer_discount = "offer_discount"
    escalate_to_human = "escalate_to_human"
    terminate_subscription = "terminate_subscription"


class CustomerTier(str, Enum):
    """Customer value tier – drives retry aggressiveness and discount eligibility."""

    starter = "starter"
    business = "business"
    enterprise = "enterprise"


class TxStatus(str, Enum):
    """Lifecycle status of a failed transaction."""

    failed = "failed"
    pending_retry = "pending_retry"
    recovered = "recovered"
    escalated = "escalated"
    abandoned = "abandoned"


# ---------------------------------------------------------------------------
# SQLModel Tables
# ---------------------------------------------------------------------------


class Transaction(SQLModel, table=True):
    """
    Root record for every failed payment event ingested by the platform.
    All other tables relate back to this via FK on ``transaction_id``.
    """

    __tablename__ = "transaction"

    id: str = Field(
        default_factory=_new_uuid,
        primary_key=True,
        description="UUID v4 transaction identifier (from Razorpay or generated).",
    )
    customer_id: str = Field(index=True, description="Opaque customer identifier.")
    amount: float = Field(description="Transaction amount in base currency units.")
    type: str = Field(default="subscription", description="Payment type (subscription, one_time, …).")
    status: TxStatus = Field(default=TxStatus.failed, description="Current lifecycle status.")
    failure_code: str = Field(description="Gateway-reported failure code (e.g. 'insufficient_funds').")
    retry_count: int = Field(default=0, ge=0, description="Number of retry attempts so far.")
    customer_value_tier: CustomerTier = Field(
        default=CustomerTier.starter,
        description="Tier used to prioritise recovery effort.",
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Diagnosis(SQLModel, table=True):
    """Gemini-generated classification for a failed transaction."""

    __tablename__ = "diagnosis"

    id: int | None = Field(default=None, primary_key=True)
    transaction_id: str = Field(
        foreign_key="transaction.id",
        index=True,
        description="FK → Transaction.id",
    )
    classification: FailureCategory = Field(description="AI-assigned failure category.")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence score [0, 1].")
    reasoning: str = Field(description="Natural-language explanation from the model.")
    created_at: datetime = Field(default_factory=_utcnow)


class RecoveryAction(SQLModel, table=True):
    """Action selected by the AI agent for a given transaction."""

    __tablename__ = "recovery_action"

    id: int | None = Field(default=None, primary_key=True)
    transaction_id: str = Field(
        foreign_key="transaction.id",
        index=True,
        description="FK → Transaction.id",
    )
    action_type: ActionType = Field(description="The recovery action to execute.")
    justification: str = Field(description="Agent reasoning for choosing this action.")
    bounds_applied: str = Field(
        description="JSON-serialised policy constraints that were enforced (e.g. max_discount_pct).",
    )
    created_at: datetime = Field(default_factory=_utcnow)


class Outcome(SQLModel, table=True):
    """Final resolution record after all recovery attempts complete."""

    __tablename__ = "outcome"

    id: int | None = Field(default=None, primary_key=True)
    transaction_id: str = Field(
        foreign_key="transaction.id",
        index=True,
        description="FK → Transaction.id",
    )
    final_status: str = Field(description="Human-readable terminal state (e.g. 'payment_succeeded').")
    amount_recovered: float = Field(default=0.0, ge=0.0, description="Amount successfully collected.")
    resolved_at: datetime = Field(default_factory=_utcnow)


class ExceptionLog(SQLModel, table=True):
    """Audit trail for escalations and unrecoverable failures."""

    __tablename__ = "exception_log"

    id: int | None = Field(default=None, primary_key=True)
    transaction_id: str = Field(
        foreign_key="transaction.id",
        index=True,
        description="FK → Transaction.id",
    )
    reason: str = Field(description="Why the normal recovery flow was bypassed.")
    escalated_at: datetime = Field(default_factory=_utcnow)
