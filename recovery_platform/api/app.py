"""
recovery_platform/api/app.py
==============================
FastAPI REST application — Phase 6 entry point.

All route handlers are plain ``def`` (not ``async def``) because
``get_session`` is a synchronous generator and ``LedgerPipeline`` is fully
synchronous.  FastAPI automatically runs plain-def handlers in a thread-pool
so the ASGI event loop is not blocked.

Run via uvicorn:
    uvicorn recovery_platform.api.app:app --reload

Endpoints
---------
POST /pipeline/run          – Trigger one recovery cycle.
GET  /metrics               – Aggregate recovery statistics.
GET  /transactions          – Paginated, filterable transaction list.
GET  /transactions/{id}/trace – Full audit trail for one transaction.
GET  /exceptions            – All escalated exceptions with tx context.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, col, select

from recovery_platform.api.schemas import (
    DiagnosisSummary,
    ExceptionLogSummary,
    ExceptionResponse,
    MetricsResponse,
    OutcomeSummary,
    PipelineRunResponse,
    RecoveryActionSummary,
    TraceResponse,
    TransactionSummary,
)
from recovery_platform.database import get_session, init_db
from recovery_platform.models import (
    CustomerTier,
    Diagnosis,
    ExceptionLog,
    Outcome,
    RecoveryAction,
    Transaction,
    TxStatus,
)
from recovery_platform.modules.diagnoser import Diagnoser
from recovery_platform.modules.executor import Executor
from recovery_platform.modules.ledger import LedgerPipeline
from recovery_platform.modules.strategist import Strategist

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure database schema is created on application startup."""
    init_db()
    yield


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Revenue Recovery Platform",
    description=(
        "Autonomous AI-powered payment failure recovery system built for "
        "Razorpay Buildathon Track 3.  Uses Google Gemini to classify payment "
        "failures and drive deterministic, policy-bounded recovery actions across "
        "the full failure taxonomy (soft decline, hard decline, technical failure, "
        "risk hold)."
    ),
    version="0.6.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Injectable component factories (overrideable in tests)
# ---------------------------------------------------------------------------


def get_diagnoser() -> Diagnoser:
    """Return a Diagnoser backed by a live GeminiClient."""
    return Diagnoser()


def get_strategist() -> Strategist:
    """Return a Strategist loaded from application policy settings."""
    return Strategist()


def get_executor() -> Executor:
    """Return an Executor loaded from application policy settings."""
    return Executor()


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
def http_exception_handler(request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code},
    )


@app.exception_handler(Exception)
def generic_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc)},
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _strip_tz(dt: datetime) -> datetime:
    """Return a naive datetime for arithmetic with SQLite-stored naive values."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _compute_mean_time_to_recovery(session: Session) -> float | None:
    """
    Return average seconds from Transaction.created_at to Outcome.resolved_at
    for all recovered Outcome rows.  Returns None if no recovered records exist.

    Uses two explicit select() queries (no ORM relationship) and computes the
    average in Python to remain compatible with SQLite's limited datetime support.
    """
    outcomes = session.exec(
        select(Outcome).where(Outcome.final_status == "recovered")
    ).all()

    if not outcomes:
        return None

    tx_ids = [o.transaction_id for o in outcomes]
    txs = {
        tx.id: tx
        for tx in session.exec(
            select(Transaction).where(col(Transaction.id).in_(tx_ids))
        ).all()
    }

    deltas: list[float] = []
    for o in outcomes:
        tx = txs.get(o.transaction_id)
        if tx is None:
            continue
        try:
            delta = (_strip_tz(o.resolved_at) - _strip_tz(tx.created_at)).total_seconds()
            if delta >= 0:
                deltas.append(delta)
        except (TypeError, AttributeError):
            continue

    return round(sum(deltas) / len(deltas), 2) if deltas else None


def format_mean_time_to_recovery(seconds: float | None) -> str | None:
    """
    Format duration in seconds into a human-readable duration string.
    e.g. 45 -> "45s", 720 -> "12 min", 15480 -> "4.3 hrs", 260441 -> "3.0 days"
    """
    if seconds is None or seconds < 0:
        return None

    if seconds < 60:
        return f"{int(round(seconds))}s"
    elif seconds < 3600:
        mins = seconds / 60.0
        return f"{round(mins)} min" if mins >= 10 else f"{mins:.1f} min"
    elif seconds < 86400:
        hrs = seconds / 3600.0
        return f"{hrs:.1f} hrs" if hrs != 1.0 else "1.0 hr"
    else:
        days = seconds / 86400.0
        return f"{days:.1f} days" if days != 1.0 else "1.0 day"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/pipeline/run", response_model=PipelineRunResponse, tags=["Pipeline"])
def run_pipeline(
    limit: int = Query(default=100, ge=1, le=1000, description="Max transactions to process"),
    session: Session = Depends(get_session),
    diagnoser: Diagnoser = Depends(get_diagnoser),
    strategist: Strategist = Depends(get_strategist),
    executor: Executor = Depends(get_executor),
) -> PipelineRunResponse:
    """
    Trigger one recovery cycle.

    Instantiates ``LedgerPipeline`` and processes up to ``limit`` unresolved
    failed transactions through the full Detector -> Diagnoser -> Strategist ->
    Executor chain.  Returns an aggregate summary of the run.
    """
    try:
        pipeline = LedgerPipeline()
        result = pipeline.run_recovery_cycle(
            session=session,
            diagnoser=diagnoser,
            strategist=strategist,
            executor=executor,
            limit=limit,
        )
        return PipelineRunResponse(**result)
    except Exception as exc:
        logger.exception("Pipeline run failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Pipeline execution error: {exc}") from exc


@app.get("/metrics", response_model=MetricsResponse, tags=["Reporting"])
def get_metrics(session: Session = Depends(get_session)) -> MetricsResponse:
    """
    Return aggregate recovery metrics.

    Computed via SQL aggregation functions — not a Python loop — over the
    Transaction and Outcome tables.
    """
    total: int = session.exec(select(func.count(Transaction.id))).one() or 0

    recovered_count: int = session.exec(
        select(func.count(Transaction.id)).where(
            Transaction.status == TxStatus.recovered
        )
    ).one() or 0

    escalated_count: int = session.exec(
        select(func.count(Transaction.id)).where(
            Transaction.status == TxStatus.escalated
        )
    ).one() or 0

    # Sum of amount_recovered for outcomes marked as recovered
    total_recovered_raw = session.exec(
        select(func.sum(Outcome.amount_recovered)).where(
            Outcome.final_status == "recovered"
        )
    ).one()
    total_recovered = float(total_recovered_raw or 0.0)

    # Sum of original transaction amounts for all non-recovered rows
    total_at_risk_raw = session.exec(
        select(func.sum(Transaction.amount)).where(
            Transaction.status != TxStatus.recovered
        )
    ).one()
    total_at_risk = float(total_at_risk_raw or 0.0)

    recovery_rate = round(recovered_count / total, 4) if total > 0 else 0.0
    mtr = _compute_mean_time_to_recovery(session)
    mtr_formatted = format_mean_time_to_recovery(mtr)

    return MetricsResponse(
        recovery_rate=recovery_rate,
        total_recovered=round(total_recovered, 2),
        total_at_risk=round(total_at_risk, 2),
        mean_time_to_recovery=mtr,
        mean_time_to_recovery_formatted=mtr_formatted,
        records_processed=total,
        escalated_count=escalated_count,
    )



@app.get(
    "/transactions/{transaction_id}/trace",
    response_model=TraceResponse,
    tags=["Transactions"],
)
def get_trace(
    transaction_id: str,
    session: Session = Depends(get_session),
) -> TraceResponse:
    """
    Return the full chronological audit trail for a single transaction.

    Performs explicit select() joins (no ORM relationship traversal) across
    Transaction, Diagnosis, RecoveryAction, Outcome, and ExceptionLog.
    All reasoning and justification strings are returned verbatim and
    untruncated — required for buildathon audit trail review.
    """
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        raise HTTPException(
            status_code=404,
            detail=f"Transaction {transaction_id!r} not found.",
        )

    diagnoses = session.exec(
        select(Diagnosis)
        .where(Diagnosis.transaction_id == transaction_id)
        .order_by(Diagnosis.created_at.asc())
    ).all()

    actions = session.exec(
        select(RecoveryAction)
        .where(RecoveryAction.transaction_id == transaction_id)
        .order_by(RecoveryAction.created_at.asc())
    ).all()

    outcomes = session.exec(
        select(Outcome)
        .where(Outcome.transaction_id == transaction_id)
        .order_by(Outcome.resolved_at.asc())
    ).all()

    exc_logs = session.exec(
        select(ExceptionLog)
        .where(ExceptionLog.transaction_id == transaction_id)
        .order_by(ExceptionLog.escalated_at.asc())
    ).all()

    return TraceResponse(
        transaction=TransactionSummary.model_validate(tx),
        diagnoses=[DiagnosisSummary.model_validate(d) for d in diagnoses],
        recovery_actions=[
            RecoveryActionSummary(
                id=a.id,
                action_type=str(a.action_type.value if hasattr(a.action_type, "value") else a.action_type),
                justification=a.justification,
                bounds_applied=json.loads(a.bounds_applied or "[]"),
                created_at=a.created_at,
            )
            for a in actions
        ],
        outcomes=[OutcomeSummary.model_validate(o) for o in outcomes],
        exception_logs=[ExceptionLogSummary.model_validate(e) for e in exc_logs],
    )


@app.get("/exceptions", response_model=list[ExceptionResponse], tags=["Reporting"])
def get_exceptions(session: Session = Depends(get_session)) -> list[ExceptionResponse]:
    """
    Return all ExceptionLog rows enriched with transaction context.

    Sorted by ``reason`` ascending so same-reason exceptions are visually
    grouped without a separate aggregation tier.  Performs two explicit
    select() queries (no ORM relationship).
    """
    exc_logs = session.exec(
        select(ExceptionLog).order_by(ExceptionLog.reason.asc())
    ).all()

    if not exc_logs:
        return []

    tx_ids = [e.transaction_id for e in exc_logs]
    tx_map = {
        tx.id: tx
        for tx in session.exec(
            select(Transaction).where(col(Transaction.id).in_(tx_ids))
        ).all()
    }

    result: list[ExceptionResponse] = []
    for e in exc_logs:
        tx = tx_map.get(e.transaction_id)
        result.append(
            ExceptionResponse(
                id=e.id,
                transaction_id=e.transaction_id,
                customer_id=tx.customer_id if tx else "unknown",
                amount=tx.amount if tx else 0.0,
                failure_code=tx.failure_code if tx else "unknown",
                customer_value_tier=(
                    str(tx.customer_value_tier.value
                        if hasattr(tx.customer_value_tier, "value")
                        else tx.customer_value_tier)
                    if tx else "unknown"
                ),
                reason=e.reason,
                escalated_at=e.escalated_at,
            )
        )

    return result


@app.get("/transactions", response_model=list[TransactionSummary], tags=["Transactions"])
def list_transactions(
    status: str | None = Query(default=None, description="Filter by TxStatus value"),
    customer_value_tier: str | None = Query(default=None, description="Filter by CustomerTier value"),
    failure_code: str | None = Query(default=None, description="Filter by exact failure_code"),
    limit: int = Query(default=50, ge=1, le=500, description="Page size"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    session: Session = Depends(get_session),
) -> list[TransactionSummary]:
    """
    Return a paginated, filterable list of transactions.

    All filters are optional and combinable.  Results are ordered by
    ``created_at`` descending (newest first).
    """
    stmt = select(Transaction)

    if status is not None:
        try:
            stmt = stmt.where(Transaction.status == TxStatus(status))
        except ValueError as err:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status value: {status!r}. "
                       f"Valid values: {[e.value for e in TxStatus]}",
            ) from err

    if customer_value_tier is not None:
        try:
            stmt = stmt.where(
                Transaction.customer_value_tier == CustomerTier(customer_value_tier)
            )
        except ValueError as err:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid customer_value_tier value: {customer_value_tier!r}. "
                       f"Valid values: {[e.value for e in CustomerTier]}",
            ) from err

    if failure_code is not None:
        stmt = stmt.where(Transaction.failure_code == failure_code)

    stmt = stmt.order_by(Transaction.created_at.desc()).offset(offset).limit(limit)
    txs = session.exec(stmt).all()

    return [TransactionSummary.model_validate(tx) for tx in txs]


# ---------------------------------------------------------------------------
# Static UI (Phase 7)
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    """Redirect bare / to the dashboard overview page."""
    return RedirectResponse(url="/ui/overview.html")


@app.get("/ui", include_in_schema=False)
def ui_root() -> RedirectResponse:
    """Redirect bare /ui to the overview page."""
    return RedirectResponse(url="/ui/overview.html")


# Mount AFTER all API routes so /ui/* doesn't shadow any API endpoint.
# html=True enables directory-index resolution.
_static_dir = __file__.replace("app.py", "static")
app.mount("/ui", StaticFiles(directory=_static_dir, html=True), name="ui")
