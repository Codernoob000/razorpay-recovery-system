"""
recovery_platform/modules/detector.py
======================================
Query helpers that identify unresolved payment failures and gate them
for downstream AI processing.

Design notes
------------
* ``fetch_unresolved_failures`` processes records FIFO (oldest first) to
  avoid indefinitely deferring early failures behind newer ones.
* ``mark_in_progress`` is an idempotent status transition – calling it
  twice on the same IDs is safe; already-updated rows are no-ops.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from recovery_platform.models import Transaction, TxStatus

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_unresolved_failures(
    session: Session,
    limit: int = 100,
) -> list[Transaction]:
    """
    Return up to *limit* ``Transaction`` rows whose ``status == TxStatus.failed``,
    ordered by ``created_at`` ascending (FIFO – oldest failure first).

    Parameters
    ----------
    session:
        Active SQLModel session.
    limit:
        Maximum number of rows to return.  Defaults to 100.

    Returns
    -------
    list[Transaction]
        Unresolved failed transactions ready for diagnosis.
    """
    stmt = (
        select(Transaction)
        .where(Transaction.status == TxStatus.failed)
        .order_by(Transaction.created_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def mark_in_progress(
    session: Session,
    transaction_ids: list[str],
) -> int:
    """
    Transition a batch of transactions from ``failed`` to ``pending_retry``
    to prevent double-processing by concurrent workers.

    Only rows currently in ``TxStatus.failed`` are updated; any IDs that
    have already been advanced are silently skipped (idempotent).

    Parameters
    ----------
    session:
        Active SQLModel session (caller is responsible for commit).
    transaction_ids:
        List of transaction IDs to lock for processing.

    Returns
    -------
    int
        Number of rows actually updated.
    """
    if not transaction_ids:
        return 0

    updated = 0
    now = datetime.now(tz=UTC)

    for tx_id in transaction_ids:
        tx = session.get(Transaction, tx_id)
        if tx is not None and tx.status == TxStatus.failed:
            tx.status = TxStatus.pending_retry
            tx.updated_at = now
            session.add(tx)
            updated += 1

    return updated


def requeue_eligible_retries(
    session: Session,
    policy=None,
    now: datetime | None = None,
) -> int:
    """
    Find transactions in ``TxStatus.pending_retry`` whose required retry delay has elapsed,
    and transition them back to ``TxStatus.failed`` so they are picked up in the next cycle.

    Parameters
    ----------
    session:
        Active SQLModel session.
    policy:
        Optional RecoveryPolicy. If None, loaded from settings.
    now:
        Optional reference datetime for testing time-dependent delays.
        Defaults to current UTC time.

    Returns
    -------
    int
        Number of transactions requeued to ``TxStatus.failed``.
    """
    if policy is None:
        from recovery_platform.config import get_settings
        policy = get_settings().recovery_policy

    if now is None:
        now = datetime.now(tz=UTC)

    ref_time = now.replace(tzinfo=None) if now.tzinfo is not None else now

    stmt = select(Transaction).where(Transaction.status == TxStatus.pending_retry)
    pending_txs = list(session.exec(stmt).all())

    requeued_count = 0
    from recovery_platform.models import Diagnosis, FailureCategory

    for tx in pending_txs:
        # Determine delay from most recent Diagnosis
        diag_stmt = (
            select(Diagnosis)
            .where(Diagnosis.transaction_id == tx.id)
            .order_by(Diagnosis.created_at.desc())
            .limit(1)
        )
        latest_diag = session.exec(diag_stmt).first()

        retry_delays = getattr(policy, "retry_delays", None)
        tech_delay = getattr(retry_delays, "technical_failure_min", 15) if retry_delays else 15
        soft_delay = getattr(retry_delays, "soft_decline_min", 60) if retry_delays else 60

        if latest_diag and latest_diag.classification == FailureCategory.technical_failure:
            delay_minutes = tech_delay
        else:
            delay_minutes = soft_delay


        tx_updated = (
            tx.updated_at.replace(tzinfo=None)
            if tx.updated_at.tzinfo is not None
            else tx.updated_at
        )
        elapsed_seconds = (ref_time - tx_updated).total_seconds()
        elapsed_minutes = elapsed_seconds / 60.0

        if elapsed_minutes >= delay_minutes:
            tx.status = TxStatus.failed
            tx.updated_at = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            session.add(tx)
            requeued_count += 1

    return requeued_count
