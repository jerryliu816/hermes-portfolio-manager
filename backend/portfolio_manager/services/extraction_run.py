"""Recording extraction attempts, and re-running them without losing history.

Re-extraction **never overwrites**. A new run is inserted and the previous one
is flipped to `is_active=false`, so the full history of how a document was read
-- by which provider, which model, which prompt version -- stays intact. That
history is what makes it possible to answer "why did this number change?" a
year later, and it is the basis of the provider benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..extraction.canonical import SCHEMA_VERSION
from ..extraction.contracts import ExtractionResult, OverviewResult
from ..ingestion.reconciler import ReconciliationReport
from ..ingestion.validator import ValidationReport
from ..models.entities import ExtractionRun, SourceFile
from ..models.enums import ImportStatus

#: Claude Opus 5 list pricing, dollars per million tokens. Used only for the
#: cost estimate in the benchmark; not a billing source of truth.
_INPUT_COST_PER_MTOK = Decimal("5")
_OUTPUT_COST_PER_MTOK = Decimal("25")


@dataclass(frozen=True)
class RunCost:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_usd: Decimal


def estimate_cost(token_usage: dict[str, int]) -> RunCost:
    """Rough cost of a run, so the benchmark can compare providers on money."""
    inp = int(token_usage.get("input_tokens", 0))
    out = int(token_usage.get("output_tokens", 0))
    cached = int(token_usage.get("cache_read_input_tokens", 0))
    usd = (Decimal(inp) * _INPUT_COST_PER_MTOK + Decimal(out) * _OUTPUT_COST_PER_MTOK) / Decimal(
        1_000_000
    )
    return RunCost(inp, out, cached, usd.quantize(Decimal("0.0001")))


def next_attempt_number(session: Session, source_file: SourceFile) -> int:
    highest = (
        session.execute(
            select(ExtractionRun.attempt_no)
            .where(ExtractionRun.source_file_id == source_file.id)
            .order_by(ExtractionRun.attempt_no.desc())
        )
        .scalars()
        .first()
    )
    return (highest or 0) + 1


def record_extraction_run(
    session: Session,
    source_file: SourceFile,
    result: ExtractionResult,
    *,
    overview: OverviewResult | None = None,
    validation: ValidationReport | None = None,
    reconciliation: ReconciliationReport | None = None,
    status: ImportStatus = ImportStatus.EXTRACTED,
    dropped_pages: tuple[int, ...] = (),
) -> ExtractionRun:
    """Insert a new run and demote every previous run for this document."""
    previous = (
        session.execute(
            select(ExtractionRun).where(
                ExtractionRun.source_file_id == source_file.id,
                ExtractionRun.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    for run in previous:
        run.is_active = False

    warnings: list[dict[str, object]] = [
        {"source": "provider", "message": w} for w in result.warnings
    ]
    if validation is not None:
        warnings.extend({"source": "validation", "message": e} for e in validation.errors)
        warnings.extend(
            {"source": "validation", "message": m} for m in validation.position_count_mismatches
        )

    reconciliation_json: dict[str, object] = {}
    if reconciliation is not None:
        reconciliation_json = {
            "summary": reconciliation.summary(),
            "failures": list(reconciliation.failure_descriptions()),
            "cash_residuals": {k: str(v) for k, v in reconciliation.cash_residuals.items()},
        }

    latency = result.latency_ms + (overview.latency_ms if overview is not None else 0)
    usage = dict(result.token_usage)
    if overview is not None:
        for key, value in overview.token_usage.items():
            usage[key] = usage.get(key, 0) + value

    run = ExtractionRun(
        source_file_id=source_file.id,
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        schema_version=result.schema_version or SCHEMA_VERSION,
        attempt_no=next_attempt_number(session, source_file),
        status=status.value,
        warnings_json=warnings,
        reconciliation_json=reconciliation_json,
        dropped_pages_json=list(dropped_pages),
        latency_ms=latency,
        token_usage=usage,
        is_active=True,
    )
    session.add(run)
    session.flush()
    return run


def active_run(session: Session, source_file: SourceFile) -> ExtractionRun | None:
    return (
        session.execute(
            select(ExtractionRun).where(
                ExtractionRun.source_file_id == source_file.id,
                ExtractionRun.is_active.is_(True),
            )
        )
        .scalars()
        .first()
    )


def run_history(session: Session, source_file: SourceFile) -> list[ExtractionRun]:
    """Every attempt at this document, newest first."""
    return list(
        session.execute(
            select(ExtractionRun)
            .where(ExtractionRun.source_file_id == source_file.id)
            .order_by(ExtractionRun.attempt_no.desc())
        ).scalars()
    )
