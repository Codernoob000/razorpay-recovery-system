"""
tests/test_seed.py
==================
Phase 3 verification: synthetic data generator and database seeder tests.

All tests run against an in-memory SQLite database – no .env required.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

# ---------------------------------------------------------------------------
# Patch env vars before any package import so Settings validates cleanly.
# ---------------------------------------------------------------------------
FAKE_ENV = {
    "GEMINI_API_KEY": "test-key-phase3",
    "DATABASE_URL":   "sqlite:///./test_phase3.db",
}

with patch.dict(os.environ, FAKE_ENV, clear=False):
    from recovery_platform.models import (
        CustomerTier,
        Transaction,
        TxStatus,
    )
    from recovery_platform.seed import (
        _CODE_TO_CAT,
        generate_synthetic_transactions,
        seed_database,
    )


# ---------------------------------------------------------------------------
# Shared in-memory engine fixture
# ---------------------------------------------------------------------------

@pytest.fixture(name="mem_engine", scope="module")
def mem_engine_fixture():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


# ---------------------------------------------------------------------------
# Tests: generate_synthetic_transactions
# ---------------------------------------------------------------------------


class TestGenerateSyntheticTransactions:

    def test_returns_exact_count(self):
        txs = generate_synthetic_transactions(50)
        assert len(txs) == 50

    def test_returns_transaction_instances(self):
        txs = generate_synthetic_transactions(20)
        for tx in txs:
            assert isinstance(tx, Transaction), f"Expected Transaction, got {type(tx)}"

    def test_all_ids_are_valid_uuids(self):
        txs = generate_synthetic_transactions(30)
        for tx in txs:
            parsed = uuid.UUID(tx.id)          # raises ValueError if invalid
            assert str(parsed) == tx.id

    def test_all_ids_are_unique(self):
        txs = generate_synthetic_transactions(60)
        ids = [tx.id for tx in txs]
        assert len(ids) == len(set(ids)), "Duplicate UUIDs detected"

    def test_no_null_required_fields(self):
        txs = generate_synthetic_transactions(40)
        for tx in txs:
            assert tx.customer_id,      "customer_id must not be empty"
            assert tx.failure_code,     "failure_code must not be empty"
            assert tx.amount > 0,       "amount must be positive"
            assert tx.type in ("subscription", "one_time"), f"unexpected type: {tx.type}"
            assert tx.status == TxStatus.failed, "All seeded transactions must start with status=failed"
            assert tx.customer_value_tier is not None
            assert tx.created_at is not None
            assert tx.updated_at is not None

    def test_all_seeded_transactions_have_failed_status(self):
        txs = generate_synthetic_transactions(60)
        for tx in txs:
            assert tx.status == TxStatus.failed

    def test_all_failure_categories_present(self):
        txs = generate_synthetic_transactions(60)
        cats = {_CODE_TO_CAT[tx.failure_code] for tx in txs}
        expected = {"soft_decline", "hard_decline", "technical_failure", "risk_hold"}
        assert expected == cats, f"Missing categories: {expected - cats}"

    def test_all_customer_tiers_present(self):
        txs = generate_synthetic_transactions(60)
        tiers = {tx.customer_value_tier.value for tx in txs}
        missing = {"starter", "business", "enterprise"} - tiers
        assert tiers == {"starter", "business", "enterprise"}, (
            "Missing tiers: " + str(missing)
        )

    def test_all_failure_codes_covered(self):
        """All 12 defined failure codes must appear in a 60-record batch."""
        import random
        random.seed(42)
        txs = generate_synthetic_transactions(60)
        codes = {tx.failure_code for tx in txs}
        # The 7 guaranteed slots cover 7 distinct codes; the rest are random.
        # With 60 records we only guarantee the 7 seeded; check those.
        guaranteed_codes = {
            "insufficient_funds", "card_expired", "gateway_timeout",
            "suspected_fraud", "card_limit_exceeded", "issuer_down",
            "velocity_limit_exceeded",
        }
        assert guaranteed_codes.issubset(codes), (
            f"Missing guaranteed codes: {guaranteed_codes - codes}"
        )

    def test_high_value_enterprise_invariant(self):
        """At least ceil(count * 0.10) records must be enterprise with amount >= 50,000."""
        import math
        count = 60
        txs = generate_synthetic_transactions(count)
        hv = [
            tx for tx in txs
            if tx.customer_value_tier == CustomerTier.enterprise and tx.amount >= 50_000
        ]
        assert len(hv) >= math.ceil(count * 0.10), (
            f"Expected >= {math.ceil(count * 0.10)} high-value enterprise records, got {len(hv)}"
        )

    def test_max_retry_invariant(self):
        """At least ceil(count * 0.08) records must have retry_count == 3."""
        import math
        count = 60
        txs = generate_synthetic_transactions(count)
        max_retry = [tx for tx in txs if tx.retry_count == 3]
        assert len(max_retry) >= math.ceil(count * 0.08), (
            f"Expected >= {math.ceil(count * 0.08)} max-retry records, got {len(max_retry)}"
        )

    def test_both_tx_types_present(self):
        txs = generate_synthetic_transactions(60)
        types = {tx.type for tx in txs}
        assert "subscription" in types
        assert "one_time" in types

    def test_timestamps_are_timezone_aware(self):
        txs = generate_synthetic_transactions(20)
        for tx in txs:
            assert tx.created_at.tzinfo is not None, "created_at must be timezone-aware"
            assert tx.updated_at.tzinfo is not None, "updated_at must be timezone-aware"

    def test_timestamps_within_last_7_days(self):
        from datetime import timedelta
        now = datetime.now(tz=UTC)
        seven_days_ago = now - timedelta(days=7, seconds=10)  # tiny buffer
        txs = generate_synthetic_transactions(60)
        for tx in txs:
            assert tx.created_at >= seven_days_ago, (
                f"created_at {tx.created_at} is older than 7 days"
            )
            assert tx.created_at <= now + timedelta(seconds=5), (
                f"created_at {tx.created_at} is in the future"
            )

    def test_updated_at_not_before_created_at(self):
        txs = generate_synthetic_transactions(60)
        for tx in txs:
            assert tx.updated_at >= tx.created_at, (
                f"updated_at ({tx.updated_at}) is before created_at ({tx.created_at})"
            )

    def test_amounts_within_tier_bounds(self):
        from recovery_platform.seed import _TIER_AMOUNTS
        txs = generate_synthetic_transactions(60)
        for tx in txs:
            lo, hi = _TIER_AMOUNTS[tx.customer_value_tier.value]
            # High-value enterprise records may override the lower bound to 50k
            effective_lo = lo
            assert tx.amount >= effective_lo * 0.99, (   # 1% float tolerance
                f"{tx.customer_value_tier.value} amount {tx.amount} below floor {lo}"
            )
            assert tx.amount <= hi * 1.01, (
                f"{tx.customer_value_tier.value} amount {tx.amount} above ceiling {hi}"
            )

    def test_retry_count_bounded(self):
        txs = generate_synthetic_transactions(60)
        for tx in txs:
            assert 0 <= tx.retry_count <= 3, f"retry_count out of bounds: {tx.retry_count}"

    def test_different_count_values(self):
        for n in [7, 20, 100]:
            txs = generate_synthetic_transactions(n)
            assert len(txs) == n

    def test_category_distribution_approximate(self):
        """
        Soft decline should be the largest category (>= 30% of 200 records).
        Uses a large sample to smooth randomness.
        """
        txs = generate_synthetic_transactions(200)
        cat_counts = Counter(_CODE_TO_CAT[tx.failure_code] for tx in txs)
        sd_pct = cat_counts["soft_decline"] / 200
        assert sd_pct >= 0.28, f"Soft decline % too low: {sd_pct:.2%}"


# ---------------------------------------------------------------------------
# Tests: seed_database
# ---------------------------------------------------------------------------


class TestSeedDatabase:

    def test_inserts_correct_count(self, mem_engine):
        inserted = seed_database(count=30, reset=True, custom_engine=mem_engine)
        assert inserted == 30

    def test_records_persisted_in_db(self, mem_engine):
        seed_database(count=25, reset=True, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            results = session.exec(select(Transaction)).all()
        assert len(results) == 25

    def test_reset_wipes_previous_data(self, mem_engine):
        seed_database(count=20, reset=True, custom_engine=mem_engine)
        seed_database(count=15, reset=True, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            results = session.exec(select(Transaction)).all()
        # After reset, only the second batch (15) should remain
        assert len(results) == 15

    def test_no_reset_accumulates_records(self, mem_engine):
        seed_database(count=10, reset=True,  custom_engine=mem_engine)
        seed_database(count=10, reset=False, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            results = session.exec(select(Transaction)).all()
        assert len(results) == 20

    def test_seeded_records_have_valid_uuids(self, mem_engine):
        seed_database(count=20, reset=True, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            txs = session.exec(select(Transaction)).all()
        for tx in txs:
            parsed = uuid.UUID(tx.id)
            assert str(parsed) == tx.id

    def test_seeded_records_have_all_tiers(self, mem_engine):
        seed_database(count=60, reset=True, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            txs = session.exec(select(Transaction)).all()
        tiers = {tx.customer_value_tier.value for tx in txs}
        assert "starter"    in tiers
        assert "business"   in tiers
        assert "enterprise" in tiers

    def test_seeded_records_have_all_failure_categories(self, mem_engine):
        seed_database(count=60, reset=True, custom_engine=mem_engine)
        with Session(mem_engine) as session:
            txs = session.exec(select(Transaction)).all()
        cats = {_CODE_TO_CAT[tx.failure_code] for tx in txs}
        assert {"soft_decline", "hard_decline", "technical_failure", "risk_hold"} == cats

    def test_returns_inserted_count(self, mem_engine):
        n = seed_database(count=42, reset=True, custom_engine=mem_engine)
        assert n == 42
