"""
recovery_platform/seed.py
==========================
Synthetic dataset generator for Razorpay payment failure scenarios.

Generates realistic, diverse Transaction records covering all failure
categories, customer tiers, and edge-case invariants required for
AI agent testing.

CLI
---
    python -m recovery_platform.seed --count 60 --reset
"""

from __future__ import annotations

import argparse
import math
import random
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel

# ---------------------------------------------------------------------------
# Ensure settings env vars are available before any package import
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _utc(dt: datetime) -> datetime:
    """Make a datetime timezone-aware (UTC) if it isn't already."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Failure scenario registry
# ---------------------------------------------------------------------------

# Each scenario: (failure_code, failure_category_string, weight)
_SCENARIOS: list[tuple[str, str, float]] = [
    # ── Soft Declines  ~40 % ─────────────────────────────────────────────
    ("insufficient_funds",       "soft_decline",      15.0),
    ("card_limit_exceeded",      "soft_decline",      13.0),
    ("authentication_failed",    "soft_decline",      12.0),
    # ── Hard Declines  ~25 % ─────────────────────────────────────────────
    ("card_expired",             "hard_decline",       9.0),
    ("invalid_account_number",   "hard_decline",       8.0),
    ("card_blocked",             "hard_decline",       8.0),
    # ── Technical Failures  ~20 % ────────────────────────────────────────
    ("gateway_timeout",          "technical_failure",  7.0),
    ("network_error",            "technical_failure",  7.0),
    ("issuer_down",              "technical_failure",  6.0),
    # ── Risk / Fraud Holds  ~15 % ────────────────────────────────────────
    ("suspected_fraud",          "risk_hold",          6.0),
    ("velocity_limit_exceeded",  "risk_hold",          5.0),
    ("blacklisted_ip",           "risk_hold",          4.0),
]

_FAILURE_CODES   = [s[0] for s in _SCENARIOS]
_WEIGHTS         = [s[2] for s in _SCENARIOS]
_CODE_TO_CAT     = {s[0]: s[1] for s in _SCENARIOS}

# Customer tier amounts (INR paisa-less, e.g. subscription MRR proxies)
_TIER_AMOUNTS: dict[str, tuple[float, float]] = {
    "starter":    (499.0,    4_999.0),
    "business":   (4_999.0,  49_999.0),
    "enterprise": (10_000.0, 2_50_000.0),
}

_TIER_WEIGHTS = [0.40, 0.35, 0.25]   # starter : business : enterprise

_TX_TYPES = ["subscription", "one_time"]
_TX_TYPE_WEIGHTS = [0.72, 0.28]


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------


def generate_synthetic_transactions(count: int = 60) -> list:
    """
    Return a list of ``Transaction`` model instances (not yet persisted).

    Invariants guaranteed
    ---------------------
    * All 4 failure categories present.
    * All 3 customer tiers present.
    * At least ``ceil(count * 0.10)`` high-value enterprise records (≥ ₹50,000).
    * At least ``ceil(count * 0.08)`` records with ``retry_count == 3``.
    * Both transaction types (subscription / one_time) present.
    * Timestamps distributed over the previous 7 days (UTC).
    """
    from recovery_platform.models import CustomerTier, Transaction, TxStatus

    now = _utcnow()
    seven_days_ago = now - timedelta(days=7)

    records: list[Transaction] = []

    # ── Seeded slots for invariants ───────────────────────────────────────
    n_hv_enterprise = math.ceil(count * 0.10)   # high-value enterprise
    n_max_retry     = math.ceil(count * 0.08)   # retry_count == 3
    n_one_time      = max(2, math.ceil(count * 0.12))  # one_time tx type

    # Track which slots get special treatment
    hv_slots      = set(random.sample(range(count), n_hv_enterprise))
    retry_slots   = set(random.sample(range(count), n_max_retry))
    onetime_slots = set(random.sample(range(count), n_one_time))

    # Guarantee coverage of all categories / tiers in the first 7 slots
    _guaranteed: list[tuple[str, str]] = [
        ("insufficient_funds",      "starter"),
        ("card_expired",            "business"),
        ("gateway_timeout",         "enterprise"),
        ("suspected_fraud",         "starter"),
        ("card_limit_exceeded",     "enterprise"),
        ("issuer_down",             "business"),
        ("velocity_limit_exceeded", "enterprise"),
    ]

    for i in range(count):
        # ── Choose failure code & tier ─────────────────────────────────
        if i < len(_guaranteed):
            failure_code, tier_str = _guaranteed[i]
        else:
            failure_code = random.choices(_FAILURE_CODES, weights=_WEIGHTS, k=1)[0]
            tier_str     = random.choices(
                ["starter", "business", "enterprise"], weights=_TIER_WEIGHTS, k=1
            )[0]

        # Override tier to enterprise for high-value slots
        if i in hv_slots:
            tier_str     = "enterprise"
            failure_code = random.choices(_FAILURE_CODES, weights=_WEIGHTS, k=1)[0]

        tier  = CustomerTier(tier_str)
        lo, hi = _TIER_AMOUNTS[tier_str]

        # High-value enterprise floor
        if i in hv_slots:
            lo = max(lo, 50_000.0)

        amount = round(random.uniform(lo, hi), 2)

        # ── Transaction type ───────────────────────────────────────────
        tx_type = "one_time" if i in onetime_slots else random.choices(
            _TX_TYPES, weights=_TX_TYPE_WEIGHTS, k=1
        )[0]

        # ── Retry count ────────────────────────────────────────────────
        retry_count = 3 if i in retry_slots else random.choices(
            [0, 1, 2], weights=[0.55, 0.30, 0.15], k=1
        )[0]

        # ── Status (all seeded as failed; terminal states written by Executor) ──
        status = TxStatus.failed

        # ── Timestamp (spread over last 7 days) ───────────────────────
        offset_seconds = random.uniform(0, 7 * 24 * 3600)
        created_at = seven_days_ago + timedelta(seconds=offset_seconds)
        # updated_at is slightly after created_at (0 – 2 hours later)
        updated_at = created_at + timedelta(seconds=random.uniform(0, 7200))

        tx = Transaction(
            id=str(uuid.uuid4()),
            customer_id=f"cust_{uuid.uuid4().hex[:8]}",
            amount=amount,
            type=tx_type,
            status=status,
            failure_code=failure_code,
            retry_count=retry_count,
            customer_value_tier=tier,
            created_at=_utc(created_at),
            updated_at=_utc(updated_at),
        )
        records.append(tx)

    return records


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def seed_database(
    count: int = 60,
    reset: bool = False,
    custom_engine=None,
) -> int:
    """
    Seed the database with synthetic transactions.

    Parameters
    ----------
    count:
        Number of Transaction records to insert.
    reset:
        If True, drop all tables then recreate them before inserting.
    custom_engine:
        Optional SQLAlchemy engine (used in tests for in-memory SQLite).

    Returns
    -------
    int
        Number of records successfully inserted.
    """
    from recovery_platform.database import engine as default_engine
    from recovery_platform.database import init_db
    from recovery_platform.models import Transaction  # noqa: F401

    eng = custom_engine or default_engine

    if reset:
        SQLModel.metadata.drop_all(eng)
        init_db(custom_engine=eng)
    else:
        # Ensure tables exist even if not resetting
        init_db(custom_engine=eng)

    transactions = generate_synthetic_transactions(count)

    with Session(eng) as session:
        for tx in transactions:
            session.add(tx)
        session.commit()

    return len(transactions)


# ---------------------------------------------------------------------------
# Summary / reporting
# ---------------------------------------------------------------------------


def _print_summary(transactions: list) -> None:
    """Print an ASCII summary table of the generated dataset."""

    code_counter: Counter = Counter()
    tier_counter: Counter = Counter()
    cat_counter:  Counter = Counter()
    type_counter: Counter = Counter()
    retry3_count  = 0
    hv_count      = 0

    for tx in transactions:
        code_counter[tx.failure_code] += 1
        tier_counter[tx.customer_value_tier.value] += 1
        cat_counter[_CODE_TO_CAT.get(tx.failure_code, "unknown")] += 1
        type_counter[tx.type] += 1
        if tx.retry_count == 3:
            retry3_count += 1
        if tx.amount >= 50_000:
            hv_count += 1

    total = len(transactions)
    sep = "+" + "-" * 50 + "+"
    wide = "+" + "=" * 50 + "+"

    def _pct(n: int) -> str:
        return f"{n / total * 100:5.1f}%"

    def _row(label: str, n: int, bar: bool = False) -> str:
        bar_str = ("#" * int(n / total * 20)) if bar else ""
        return f"| {label:<30s} {n:3d}  {_pct(n)}  {bar_str:<10s}|"

    print()
    print(wide)
    print(f"| {'Synthetic Dataset Summary':<48s} |")
    print(wide)
    print(f"| {'Total records generated':<30s} {total:<18d} |")
    print(sep)

    # Failure category distribution
    print(f"| {'Failure Category Distribution':<48s} |")
    print(sep)
    for cat in ["soft_decline", "hard_decline", "technical_failure", "risk_hold"]:
        n = cat_counter[cat]
        print(_row(cat, n, bar=True))

    print(sep)

    # Customer tier distribution
    print(f"| {'Customer Tier Distribution':<48s} |")
    print(sep)
    for tier in ["starter", "business", "enterprise"]:
        n = tier_counter[tier]
        print(_row(tier, n, bar=True))

    print(sep)

    # Failure code breakdown
    print(f"| {'Failure Code Breakdown':<48s} |")
    print(sep)
    for code, n in sorted(code_counter.items(), key=lambda x: -x[1]):
        print(f"| {code:<30s} {n:3d}  {_pct(n)}           |")

    print(sep)

    # Transaction type
    print(f"| {'Transaction Type':<48s} |")
    print(sep)
    for ttype in ["subscription", "one_time"]:
        n = type_counter[ttype]
        print(f"| {ttype:<30s} {n:3d}  {_pct(n)}           |")

    print(sep)

    # Edge-case invariants
    print(f"| {'Edge-Case Invariants':<48s} |")
    print(sep)
    print(f"| {'High-value (>= Rs.50,000)':<30s} {hv_count:3d}  {_pct(hv_count)}           |")
    print(f"| {'Max retry (count == 3)':<30s} {retry3_count:3d}  {_pct(retry3_count)}           |")

    print(wide)
    print()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Recovery Platform database with synthetic payment failures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m recovery_platform.seed --count 60 --reset
  python -m recovery_platform.seed --count 120
        """,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=60,
        help="Number of Transaction records to generate (default: 60).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate all tables before seeding.",
    )
    args = parser.parse_args()

    if args.count < 7:
        parser.error("--count must be at least 7 to guarantee category/tier coverage.")

    print(f"\n[*] Generating {args.count} synthetic transactions (reset={args.reset}) ...")

    # Generate first so we can show the summary before writing to DB
    transactions = generate_synthetic_transactions(args.count)
    _print_summary(transactions)

    inserted = seed_database(count=args.count, reset=args.reset)
    print(f"[OK] {inserted} records written to database.\n")


if __name__ == "__main__":
    _cli()
