"""
recovery_platform/modules/diagnoser.py
========================================
AI-powered payment failure diagnoser.

Builds a structured diagnostic prompt from a ``Transaction`` record, calls
``GeminiClient.generate_structured_json``, and persists the result as a
``Diagnosis`` row.  Every failure path converges to a safe ``risk_hold``
fallback so the pipeline never crashes due to an LLM error.

Failure taxonomy
----------------
soft_decline      – Transient customer-side issue (insufficient funds, limit).
hard_decline      – Permanent card/account problem (expired, blocked, invalid).
technical_failure – Infrastructure / gateway fault (timeout, network, issuer).
risk_hold         – Fraud/compliance signal (suspected fraud, velocity, blacklist).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from recovery_platform.models import Diagnosis, FailureCategory, Transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic schema for Gemini structured output
# ---------------------------------------------------------------------------


class DiagnosisOutput(BaseModel):
    """Structured output schema enforced by the Gemini API JSON mode."""

    classification: FailureCategory
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str

    # Convenience class-level fallback
    @classmethod
    def safety_fallback(cls) -> DiagnosisOutput:
        return cls(
            classification=FailureCategory.risk_hold,
            confidence=0.0,
            reasoning=(
                "Automated diagnosis unavailable. "
                "Fallback safety hold invoked."
            ),
        )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
You are an expert payment-failure analyst for a SaaS subscription platform.
Analyse the payment failure record below and classify it into EXACTLY ONE of
these four categories:
  - soft_decline:      Transient customer-side issue (insufficient funds,
                       credit-limit exceeded, authentication failed).
  - hard_decline:      Permanent card/account problem (card expired, invalid
                       account number, card permanently blocked).
  - technical_failure: Infrastructure or gateway fault (gateway timeout,
                       network error, issuer temporarily down).
  - risk_hold:         Fraud or compliance signal (suspected fraud, velocity
                       limit exceeded, blacklisted IP, AML flag).

Return your answer as JSON with the following fields:
  classification : one of the four category strings above
  confidence     : float between 0.0 and 1.0
  reasoning      : concise explanation (1-3 sentences)

Do NOT include any markdown, code fences, or additional keys.
"""


def _build_prompt(tx: Transaction) -> str:
    return (
        f"{_SYSTEM_INSTRUCTION}\n\n"
        f"Payment Failure Record:\n"
        f"  failure_code         : {tx.failure_code}\n"
        f"  retry_count          : {tx.retry_count}\n"
        f"  customer_value_tier  : {tx.customer_value_tier.value}\n"
        f"  amount               : {tx.amount}\n"
        f"  transaction_type     : {tx.type}\n"
    )


# ---------------------------------------------------------------------------
# Diagnoser
# ---------------------------------------------------------------------------


class Diagnoser:
    """
    Wraps ``GeminiClient`` to produce a ``DiagnosisOutput`` for a transaction.

    Parameters
    ----------
    client:
        ``GeminiClient`` instance.  If *None*, one is created from settings.
    """

    def __init__(self, client=None) -> None:
        if client is None:
            from recovery_platform.modules.llm_client import GeminiClient
            self._client = GeminiClient()
        else:
            self._client = client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def diagnose_transaction(self, tx: Transaction) -> DiagnosisOutput:
        """
        Classify *tx* into a ``DiagnosisOutput``.

        All exceptions are caught and the method always returns a valid
        ``DiagnosisOutput``; on any error the returned object will have
        ``confidence=0.0`` and ``classification=risk_hold`` (safety fallback).

        Parameters
        ----------
        tx:
            ``Transaction`` instance to diagnose.

        Returns
        -------
        DiagnosisOutput
            Structured diagnostic result (never raises).
        """
        prompt = _build_prompt(tx)
        try:
            result = self._client.generate_structured_json(prompt, DiagnosisOutput)
            logger.info(
                "Diagnosed tx=%s -> %s (confidence=%.2f)",
                tx.id, result.classification.value, result.confidence,
            )
            return result
        except Exception as exc:
            logger.error(
                "Diagnosis failed for tx=%s, invoking safety fallback. Error: %s",
                tx.id, exc,
            )
            return DiagnosisOutput.safety_fallback()

    def persist_diagnosis(
        self,
        tx: Transaction,
        output: DiagnosisOutput,
        session,
    ) -> Diagnosis:
        """
        Persist a ``DiagnosisOutput`` as a ``Diagnosis`` DB row.

        Parameters
        ----------
        tx:
            Source transaction.
        output:
            Structured diagnosis result.
        session:
            Active SQLModel session (caller commits).

        Returns
        -------
        Diagnosis
            The persisted (not yet committed) ``Diagnosis`` instance.
        """
        diag = Diagnosis(
            transaction_id=tx.id,
            classification=output.classification,
            confidence=output.confidence,
            reasoning=output.reasoning,
            created_at=datetime.now(tz=UTC),
        )
        session.add(diag)
        return diag
