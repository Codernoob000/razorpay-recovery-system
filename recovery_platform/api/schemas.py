"""
recovery_platform/api/schemas.py
==================================
Pydantic response models for the FastAPI REST layer.

All schemas are intentionally decoupled from the SQLModel table classes so
the API surface remains stable even if the DB schema evolves.

Design notes
------------
* ``from_attributes=True`` is set on the ORM-mapped schemas so they can be
  constructed directly from SQLModel instances via ``model_validate``.
* ``Outcome.final_status`` is a plain ``str`` (not ``TxStatus``) because the
  Executor writes ``.value`` strings.  Schemas follow this contract exactly.
* ``RecoveryActionSummary.bounds_applied`` is ``list[str]``; callers must
  ``json.loads`` the raw DB column before constructing this model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Shared base with ORM-mode enabled
# ---------------------------------------------------------------------------


class _OrmBase(BaseModel):
    """Pydantic base that reads fields from SQLModel ORM objects."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Flat entity summaries
# ---------------------------------------------------------------------------


class TransactionSummary(_OrmBase):
    """Flat representation of a Transaction row."""

    id: str
    customer_id: str
    amount: float
    type: str
    status: str                    # TxStatus value as plain string
    failure_code: str
    retry_count: int
    customer_value_tier: str       # CustomerTier value as plain string
    created_at: datetime
    updated_at: datetime


class DiagnosisSummary(_OrmBase):
    """Flat representation of a Diagnosis row — reasoning is verbatim."""

    id: int
    classification: str            # FailureCategory value
    confidence: float
    reasoning: str                 # untruncated for audit trail
    created_at: datetime


class RecoveryActionSummary(BaseModel):
    """
    Flat representation of a RecoveryAction row.

    ``bounds_applied`` is the *deserialized* list (route handler calls
    ``json.loads`` on the raw DB column before constructing this model).
    ``justification`` is verbatim for the audit trail.
    """

    id: int
    action_type: str               # ActionType value
    justification: str             # untruncated for audit trail
    bounds_applied: list[str]      # deserialized from JSON in the route handler
    created_at: datetime


class OutcomeSummary(_OrmBase):
    """Flat representation of an Outcome row."""

    id: int
    final_status: str              # plain str, NOT TxStatus — handle explicitly
    amount_recovered: float
    resolved_at: datetime


class ExceptionLogSummary(_OrmBase):
    """Flat representation of an ExceptionLog row."""

    id: int
    reason: str
    escalated_at: datetime


# ---------------------------------------------------------------------------
# Composite / nested response bodies
# ---------------------------------------------------------------------------


class TraceResponse(BaseModel):
    """
    Full chronological audit trail for a single transaction.

    All lists are ordered by created_at/resolved_at/escalated_at ascending.
    All text fields (reasoning, justification) are untruncated.
    """

    transaction: TransactionSummary
    diagnoses: list[DiagnosisSummary]
    recovery_actions: list[RecoveryActionSummary]
    outcomes: list[OutcomeSummary]
    exception_logs: list[ExceptionLogSummary]


class MetricsResponse(BaseModel):
    """
    Aggregate recovery metrics computed in a single SQL pass.

    mean_time_to_recovery is in seconds; None when no recovered
    transactions exist yet (cold start).
    """

    recovery_rate: float           # recovered / total, 0.0-1.0
    total_recovered: float         # sum of Outcome.amount_recovered (recovered rows)
    total_at_risk: float           # sum of Transaction.amount (non-recovered rows)
    mean_time_to_recovery: float | None   # seconds, None if no recoveries
    mean_time_to_recovery_formatted: str | None = None  # human-readable duration (e.g. '3.0 days', '4.3 hrs')
    records_processed: int         # total Transaction count
    escalated_count: int


class ExceptionResponse(BaseModel):
    """
    ExceptionLog row enriched with transaction context.

    Results are sorted by reason ascending so same-reason exceptions
    appear together (visual grouping without a separate aggregation tier).
    """

    id: int
    transaction_id: str
    customer_id: str
    amount: float
    failure_code: str
    customer_value_tier: str
    reason: str
    escalated_at: datetime


class PipelineRunResponse(BaseModel):
    """
    Summary of one LedgerPipeline.run_recovery_cycle execution.

    Field names match the dict keys returned by run_recovery_cycle exactly
    so PipelineRunResponse(**result) always works.
    """

    processed: int
    recovered_count: int
    escalated_count: int
    requeued_count: int = 0
    total_recovered_amount: float

