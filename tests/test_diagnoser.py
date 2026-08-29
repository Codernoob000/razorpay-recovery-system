"""
tests/test_diagnoser.py
========================
Phase 4 verification: Diagnoser structured-output parsing, fallback
safety, and failure-code-to-category mapping.

All LLM calls are mocked – no real Gemini API key is needed.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-diagnoser",
    "DATABASE_URL":   "sqlite:///./test_diagnoser.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.models import CustomerTier, FailureCategory, Transaction, TxStatus
    from recovery_platform.modules.diagnoser import Diagnoser, DiagnosisOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _make_tx(failure_code: str, tier: CustomerTier = CustomerTier.enterprise,
             amount: float = 5000.0, retry_count: int = 0) -> Transaction:
    return Transaction(
        id=str(uuid.uuid4()),
        customer_id="cust_test",
        amount=amount,
        type="subscription",
        status=TxStatus.failed,
        failure_code=failure_code,
        retry_count=retry_count,
        customer_value_tier=tier,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )


def _mock_client(output: DiagnosisOutput) -> MagicMock:
    """Return a fake GeminiClient that always returns *output*."""
    client = MagicMock()
    client.generate_structured_json.return_value = output
    return client


def _raising_client(exc: Exception) -> MagicMock:
    """Return a fake GeminiClient that always raises *exc*."""
    client = MagicMock()
    client.generate_structured_json.side_effect = exc
    return client


# ---------------------------------------------------------------------------
# DiagnosisOutput unit tests
# ---------------------------------------------------------------------------


class TestDiagnosisOutput:

    def test_valid_construction(self):
        d = DiagnosisOutput(
            classification=FailureCategory.soft_decline,
            confidence=0.9,
            reasoning="Test reasoning.",
        )
        assert d.classification == FailureCategory.soft_decline
        assert d.confidence == pytest.approx(0.9)

    def test_confidence_lower_bound(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DiagnosisOutput(
                classification=FailureCategory.hard_decline,
                confidence=-0.1,
                reasoning="bad",
            )

    def test_confidence_upper_bound(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DiagnosisOutput(
                classification=FailureCategory.soft_decline,
                confidence=1.01,
                reasoning="bad",
            )

    def test_safety_fallback_values(self):
        fb = DiagnosisOutput.safety_fallback()
        assert fb.classification == FailureCategory.risk_hold
        assert fb.confidence == pytest.approx(0.0)
        assert "fallback" in fb.reasoning.lower()

    def test_all_categories_valid(self):
        for cat in FailureCategory:
            d = DiagnosisOutput(classification=cat, confidence=0.5, reasoning="ok")
            assert d.classification == cat


# ---------------------------------------------------------------------------
# Diagnoser.diagnose_transaction – happy path
# ---------------------------------------------------------------------------


class TestDiagnoserHappyPath:

    def test_returns_diagnosis_output_type(self):
        expected = DiagnosisOutput(
            classification=FailureCategory.soft_decline,
            confidence=0.95,
            reasoning="Card limit exceeded.",
        )
        diagnoser = Diagnoser(client=_mock_client(expected))
        tx = _make_tx("card_limit_exceeded")

        result = diagnoser.diagnose_transaction(tx)
        assert isinstance(result, DiagnosisOutput)

    def test_passes_prompt_to_client(self):
        expected = DiagnosisOutput(
            classification=FailureCategory.technical_failure,
            confidence=0.8,
            reasoning="Gateway timeout.",
        )
        client = _mock_client(expected)
        diagnoser = Diagnoser(client=client)
        tx = _make_tx("gateway_timeout")

        diagnoser.diagnose_transaction(tx)

        assert client.generate_structured_json.called
        call_args = client.generate_structured_json.call_args
        prompt_arg = call_args[0][0]  # first positional arg = prompt
        assert "gateway_timeout" in prompt_arg

    def test_prompt_contains_all_tx_fields(self):
        client = _mock_client(DiagnosisOutput.safety_fallback())
        diagnoser = Diagnoser(client=client)
        tx = _make_tx("suspected_fraud", tier=CustomerTier.enterprise, amount=75000.0, retry_count=2)

        diagnoser.diagnose_transaction(tx)

        prompt = client.generate_structured_json.call_args[0][0]
        assert "suspected_fraud"  in prompt
        assert "enterprise"       in prompt
        assert "75000"            in prompt
        assert "2"                in prompt    # retry_count

    def test_schema_arg_is_diagnosis_output(self):
        client = _mock_client(DiagnosisOutput.safety_fallback())
        diagnoser = Diagnoser(client=client)
        tx = _make_tx("insufficient_funds")

        diagnoser.diagnose_transaction(tx)

        schema_arg = client.generate_structured_json.call_args[0][1]
        assert schema_arg is DiagnosisOutput

    def test_classification_propagated(self):
        for cat in FailureCategory:
            expected = DiagnosisOutput(classification=cat, confidence=0.88, reasoning="test")
            diagnoser = Diagnoser(client=_mock_client(expected))
            result = diagnoser.diagnose_transaction(_make_tx("any_code"))
            assert result.classification == cat


# ---------------------------------------------------------------------------
# Diagnoser.diagnose_transaction – fallback on LLM errors
# ---------------------------------------------------------------------------


class TestDiagnoserFallback:

    def test_timeout_error_returns_fallback(self):
        diagnoser = Diagnoser(client=_raising_client(TimeoutError("Request timed out")))
        result = diagnoser.diagnose_transaction(_make_tx("gateway_timeout"))

        assert result.classification == FailureCategory.risk_hold
        assert result.confidence == pytest.approx(0.0)
        assert "fallback" in result.reasoning.lower()

    def test_connection_error_returns_fallback(self):
        diagnoser = Diagnoser(client=_raising_client(ConnectionError("Network unreachable")))
        result = diagnoser.diagnose_transaction(_make_tx("network_error"))

        assert result.classification == FailureCategory.risk_hold
        assert result.confidence == pytest.approx(0.0)

    def test_generic_exception_returns_fallback(self):
        diagnoser = Diagnoser(client=_raising_client(RuntimeError("Unknown LLM error")))
        result = diagnoser.diagnose_transaction(_make_tx("card_blocked"))

        assert result.classification == FailureCategory.risk_hold

    def test_rate_limit_429_returns_fallback(self):
        exc = Exception("429 Too Many Requests: rate limit exceeded")
        diagnoser = Diagnoser(client=_raising_client(exc))
        result = diagnoser.diagnose_transaction(_make_tx("insufficient_funds"))

        assert result.classification == FailureCategory.risk_hold
        assert result.confidence == pytest.approx(0.0)

    def test_service_unavailable_503_returns_fallback(self):
        exc = Exception("503 Service Unavailable")
        diagnoser = Diagnoser(client=_raising_client(exc))
        result = diagnoser.diagnose_transaction(_make_tx("issuer_down"))

        assert result.classification == FailureCategory.risk_hold

    def test_value_error_returns_fallback(self):
        """Malformed / unparseable LLM response (e.g. invalid JSON)."""
        diagnoser = Diagnoser(client=_raising_client(ValueError("Invalid JSON")))
        result = diagnoser.diagnose_transaction(_make_tx("card_expired"))

        assert result.classification == FailureCategory.risk_hold

    def test_fallback_never_raises(self):
        """Regardless of error type, diagnose_transaction must not propagate.

        Note: KeyboardInterrupt (BaseException) is intentionally excluded;
        it is not an LLM error and it is correct to let it propagate.
        """
        for exc in [
            TimeoutError("t/o"),
            ConnectionError("conn"),
            ValueError("bad json"),
            RuntimeError("crash"),
            Exception("generic"),
        ]:
            diagnoser = Diagnoser(client=_raising_client(exc))
            try:
                result = diagnoser.diagnose_transaction(_make_tx("any"))
                assert result.classification == FailureCategory.risk_hold
            except BaseException as propagated:
                pytest.fail(f"diagnose_transaction raised {propagated!r} instead of returning fallback")


# ---------------------------------------------------------------------------
# Failure-code -> expected diagnosis category mapping
# ---------------------------------------------------------------------------


class TestFailureCodeMapping:
    """
    Verify that mock responses with the expected classification for each
    failure code are correctly propagated through the diagnoser.
    These tests document the intended classification contract.
    """

    _EXPECTED: list[tuple[str, FailureCategory]] = [
        # Soft declines
        ("insufficient_funds",      FailureCategory.soft_decline),
        ("card_limit_exceeded",     FailureCategory.soft_decline),
        ("authentication_failed",   FailureCategory.soft_decline),
        # Hard declines
        ("card_expired",            FailureCategory.hard_decline),
        ("invalid_account_number",  FailureCategory.hard_decline),
        ("card_blocked",            FailureCategory.hard_decline),
        # Technical failures
        ("gateway_timeout",         FailureCategory.technical_failure),
        ("network_error",           FailureCategory.technical_failure),
        ("issuer_down",             FailureCategory.technical_failure),
        # Risk holds
        ("suspected_fraud",         FailureCategory.risk_hold),
        ("velocity_limit_exceeded", FailureCategory.risk_hold),
        ("blacklisted_ip",          FailureCategory.risk_hold),
    ]

    @pytest.mark.parametrize("failure_code,expected_cat", _EXPECTED)
    def test_code_maps_to_category(self, failure_code: str, expected_cat: FailureCategory):
        """
        A mocked LLM that returns the expected category must survive the
        full diagnoser pipeline and return the correct classification.
        """
        mock_output = DiagnosisOutput(
            classification=expected_cat,
            confidence=0.90,
            reasoning=f"Test: {failure_code} -> {expected_cat.value}",
        )
        diagnoser = Diagnoser(client=_mock_client(mock_output))
        tx = _make_tx(failure_code)

        result = diagnoser.diagnose_transaction(tx)

        assert result.classification == expected_cat, (
            f"{failure_code} expected {expected_cat.value}, got {result.classification.value}"
        )
        assert result.confidence == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# Diagnoser.persist_diagnosis
# ---------------------------------------------------------------------------


class TestPersistDiagnosis:

    def test_creates_diagnosis_row(self):
        from sqlmodel import Session, SQLModel, create_engine


        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)

        tx = _make_tx("insufficient_funds")
        output = DiagnosisOutput(
            classification=FailureCategory.soft_decline,
            confidence=0.92,
            reasoning="Card limit hit.",
        )
        diagnoser = Diagnoser(client=_mock_client(output))

        diag_id = None
        tx_id   = tx.id   # capture before session closes

        with Session(engine) as session:
            session.add(tx)
            session.commit()
            diag = diagnoser.persist_diagnosis(tx, output, session)
            session.commit()
            session.refresh(diag)
            diag_id             = diag.id
            diag_transaction_id = diag.transaction_id
            diag_classification = diag.classification
            diag_confidence     = diag.confidence
            diag_reasoning      = diag.reasoning

        assert diag_id is not None
        assert diag_transaction_id == tx_id
        assert diag_classification == FailureCategory.soft_decline
        assert diag_confidence == pytest.approx(0.92)
        assert diag_reasoning == "Card limit hit."
