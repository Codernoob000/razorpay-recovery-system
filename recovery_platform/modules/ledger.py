"""
recovery_platform/modules/ledger.py
=====================================
Central orchestrator: Detector -> Diagnoser -> Strategist -> Executor.

``LedgerPipeline`` pulls unresolved failures, runs the full recovery chain
for each one, and returns an aggregate summary dict.

The pipeline is intentionally transactional per-record: a failure on one
transaction is logged and skipped rather than rolling back the whole batch.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session

from recovery_platform.models import TxStatus
from recovery_platform.modules.detector import (
    fetch_unresolved_failures,
    requeue_eligible_retries,
)

logger = logging.getLogger(__name__)


class LedgerPipeline:
    """
    Orchestrates one full recovery cycle.

    Inject pre-built ``Diagnoser``, ``Strategist``, and ``Executor``
    instances so the pipeline is fully unit-testable via mocks.
    """

    def run_recovery_cycle(
        self,
        session: Session,
        diagnoser,
        strategist,
        executor,
        limit: int = 100,
        now: datetime | None = None,
    ) -> dict:
        """
        Execute one recovery cycle over at most *limit* unresolved failures.

        Processing order
        ----------------
        1. ``requeue_eligible_retries`` – Transition eligible pending_retry rows back to failed.
        2. ``fetch_unresolved_failures`` – FIFO, status == failed only.
        3. For each transaction:
           a. Diagnose  -> ``DiagnosisOutput``
           b. Persist   -> ``Diagnosis`` row
           c. Strategise -> ``(ActionType, justification, bounds)``
           d. Execute   -> ``(TxStatus, amount_recovered)``
        4. Commit the entire batch (owned by caller); single-record errors are caught, logged,
           and excluded from counts.

        Parameters
        ----------
        session:
            Active SQLModel session (caller commits).
        diagnoser:
            ``Diagnoser`` instance.
        strategist:
            ``Strategist`` instance.
        executor:
            ``Executor`` instance.
        limit:
            Maximum transactions to process per cycle.
        now:
            Optional reference datetime for testing time-dependent retry delays.

        Returns
        -------
        dict
            ``{
                "processed":             int,
                "recovered_count":       int,
                "escalated_count":       int,
                "requeued_count":        int,
                "total_recovered_amount": float,
            }``
        """
        policy = getattr(executor, "_policy", None) or getattr(strategist, "_policy", None)
        requeued_count = requeue_eligible_retries(session, policy=policy, now=now)

        transactions = fetch_unresolved_failures(session, limit=limit)

        processed             = 0
        recovered_count       = 0
        escalated_count       = 0
        total_recovered_amount = 0.0

        for tx in transactions:
            try:
                # ── Step 1: Diagnose ─────────────────────────────────
                diag_output = diagnoser.diagnose_transaction(tx)

                # ── Step 2: Persist Diagnosis ────────────────────────
                diagnoser.persist_diagnosis(tx, diag_output, session)

                # ── Step 3: Evaluate strategy ────────────────────────
                action_type, justification, bounds_applied = (
                    strategist.evaluate_strategy(tx, diag_output, session=session)
                )

                # ── Step 4: Execute action ───────────────────────────
                new_status, amount_recovered = executor.execute_action(
                    session=session,
                    tx=tx,
                    action_type=action_type,
                    bounds_applied=bounds_applied,
                    justification=justification,
                )

                # ── Aggregate stats ──────────────────────────────────
                processed += 1

                if new_status == TxStatus.recovered:
                    recovered_count       += 1
                    total_recovered_amount += amount_recovered

                if new_status == TxStatus.escalated:
                    escalated_count += 1

            except Exception as exc:
                logger.error(
                    "LedgerPipeline: unhandled error processing tx=%s – skipping. %s",
                    getattr(tx, "id", "?"), exc,
                    exc_info=True,
                )
                continue

        summary = {
            "processed":              processed,
            "recovered_count":        recovered_count,
            "escalated_count":        escalated_count,
            "requeued_count":         requeued_count,
            "total_recovered_amount": round(total_recovered_amount, 2),
        }
        logger.info("LedgerPipeline cycle complete: %s", summary)
        return summary
