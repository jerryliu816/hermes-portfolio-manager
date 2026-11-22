"""Investment policy endpoints.

The policy is edited as YAML because that is what survives review: comments,
ordering and the user's own reasoning are preserved verbatim rather than
regenerated from a normalised structure.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from ...analytics import load_portfolio
from ...analytics.policy_drift import PolicyAssessment, assess
from ...schemas.responses import (
    ClassDriftOut,
    ConstraintResultOut,
    PlacementObservationOut,
    PolicyAssessmentOut,
    PolicyOut,
    PolicyVersionOut,
)
from ...services.policy import (
    STARTER_POLICY_YAML,
    PolicyError,
    active_policy,
    policy_history,
    save_policy_yaml,
)
from ..deps import SessionDep, resolve_snapshot

router = APIRouter(prefix="/policy", tags=["policy"])


class PolicyYamlIn(BaseModel):
    yaml: str = Field(description="The complete policy document as YAML")


@router.get("", response_model=PolicyOut)
def get_policy(session: SessionDep) -> PolicyOut:
    stored = active_policy(session)
    if stored is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no investment policy has been set; GET /policy/starter for a template",
        )
    return PolicyOut(
        name=stored.record.name,
        version=stored.record.version,
        is_active=stored.record.is_active,
        document=stored.record.document_json,
        raw_yaml=stored.record.raw_yaml,
    )


@router.get("/starter", response_class=Response, responses={200: {"content": {"text/yaml": {}}}})
def get_starter_policy() -> Response:
    """A commented template that states its own assumptions rather than advising."""
    return Response(content=STARTER_POLICY_YAML, media_type="text/yaml")


@router.get("/yaml", response_class=Response, responses={200: {"content": {"text/yaml": {}}}})
def get_policy_yaml(session: SessionDep) -> Response:
    stored = active_policy(session)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no investment policy has been set")
    return Response(content=stored.record.raw_yaml or "", media_type="text/yaml")


@router.put("", response_model=PolicyOut, status_code=status.HTTP_200_OK)
def put_policy(payload: PolicyYamlIn, session: SessionDep) -> PolicyOut:
    """Validate and store a policy as a new version.

    Saving never overwrites: the previous version is retired, not replaced, so
    "what was my policy when I made that decision?" stays answerable.
    """
    try:
        stored = save_policy_yaml(session, payload.yaml)
    except PolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return PolicyOut(
        name=stored.record.name,
        version=stored.record.version,
        is_active=stored.record.is_active,
        document=stored.record.document_json,
        raw_yaml=stored.record.raw_yaml,
    )


@router.get("/versions", response_model=list[PolicyVersionOut])
def list_versions(session: SessionDep, name: str = "default") -> list[PolicyVersionOut]:
    return [
        PolicyVersionOut(
            name=record.name,
            version=record.version,
            is_active=record.is_active,
            created_at=record.created_at,
        )
        for record in policy_history(session, name)
    ]


def _assessment_out(assessment: PolicyAssessment) -> PolicyAssessmentOut:
    def drift_out(items: object) -> list[ClassDriftOut]:
        return [
            ClassDriftOut(
                asset_class=d.asset_class,
                label=d.label,
                target=d.target,
                actual=d.actual,
                band=d.band,
                value=d.value,
                drift=d.drift,
                is_in_band=d.is_in_band,
                direction=d.direction,
                rebalance_value=d.rebalance_value(assessment.total_value),
            )
            for d in items  # type: ignore[attr-defined]
        ]

    def constraints_out(items: object) -> list[ConstraintResultOut]:
        return [
            ConstraintResultOut(
                id=c.constraint.id,
                kind=c.constraint.kind.value,
                description=c.constraint.description,
                limit=c.constraint.limit,
                actual=c.actual,
                satisfied=c.satisfied,
                breach=c.breach,
                detail=c.detail,
            )
            for c in items  # type: ignore[attr-defined]
        ]

    return PolicyAssessmentOut(
        policy_name=assessment.policy_name,
        policy_version=assessment.policy_version,
        total_value=assessment.total_value,
        classified_value=assessment.classified_value,
        unclassified_value=assessment.unclassified_value,
        unclassified_fraction=assessment.unclassified_fraction,
        is_compliant=assessment.is_compliant,
        is_provisional=assessment.is_provisional,
        drift=drift_out(assessment.drift),
        out_of_band=drift_out(assessment.out_of_band),
        constraints=constraints_out(assessment.constraints),
        breaches=constraints_out(assessment.breaches),
        placement=[
            PlacementObservationOut(
                asset_class=p.asset_class,
                preferred=p.preferred,
                value_in_preferred=p.value_in_preferred,
                value_elsewhere=p.value_elsewhere,
                fraction_in_preferred=p.fraction_in_preferred,
            )
            for p in assessment.placement
        ],
        caveats=assessment.caveats,
        data_caveats=assessment.data_caveats,
        policy_notes=assessment.policy_notes,
    )


@router.get("/assessment", response_model=PolicyAssessmentOut)
def get_assessment(session: SessionDep, snapshot_id: str | None = None) -> PolicyAssessmentOut:
    """How the portfolio stands against the policy, with the caveats that apply."""
    stored = active_policy(session)
    if stored is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no investment policy has been set, so there is nothing to assess against",
        )
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    return _assessment_out(assess(view, stored.document))
