"""The REST API.

Two guarantees are asserted directly rather than assumed, because both would
fail silently: money must never be a JSON number, and a real brokerage account
number must never leave the server.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from portfolio_manager.api.deps import get_session
from portfolio_manager.config import Settings, get_settings
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.main import create_app
from portfolio_manager.services.importer import import_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)

#: Field names whose values must never be serialised as JSON numbers.
MONEY_FIELDS = {
    "total_value",
    "holdings_value",
    "cash_value",
    "market_value",
    "cost_basis",
    "unrealized_gain_loss",
    "price",
    "quantity",
    "value",
    "before",
    "after",
    "delta",
    "stale_value",
    "unclassified_value",
    "missing_basis_value",
    "aggregated_value",
    "covered_value",
    "uncovered_value",
    "unrealized_gain",
    "excluded_value",
    "cash_balance",
    "settlement_value",
    "taxable_value",
    "unconfirmed_value",
    "before_value",
    "after_value",
    "value_change",
    "total",
    "stale_fraction",
    "fraction",
    "hhi",
}


@pytest.fixture
def client(session: Session, tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)
    session.flush()

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((f"{path}.{key}", value))
            found.extend(_walk(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}[{index}]"))
    return found


# --- the two guarantees ---------------------------------------------------


def test_no_money_field_is_a_json_number_in_any_response(client: TestClient) -> None:
    """A JSON number is a float in most clients, and floats cannot hold money."""
    endpoints = [
        "/api/v1/portfolio/summary",
        "/api/v1/portfolio/allocation/asset-class",
        "/api/v1/portfolio/allocation/tax-treatment",
        "/api/v1/portfolio/cash",
        "/api/v1/portfolio/concentration",
        "/api/v1/portfolio/gains",
        "/api/v1/portfolio/securities",
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/snapshots",
    ]
    offenders: list[str] = []
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        for path, value in _walk(response.json()):
            name = path.rsplit(".", 1)[-1]
            if (
                name in MONEY_FIELDS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                offenders.append(f"{endpoint}{path} = {value!r}")
    assert not offenders, "money serialised as a JSON number:\n" + "\n".join(offenders)


def test_the_openapi_schema_promises_strings_for_money(client: TestClient) -> None:
    """The contract must match the wire, or generated clients parse floats."""
    schema = client.get("/openapi.json").json()
    offenders: list[str] = []
    for name, definition in schema["components"]["schemas"].items():
        for field, spec in definition.get("properties", {}).items():
            if field not in MONEY_FIELDS:
                continue
            types = {spec.get("type")} | {option.get("type") for option in spec.get("anyOf", [])}
            if "number" in types or "integer" in types:
                offenders.append(f"{name}.{field}: {types}")
    assert not offenders, "OpenAPI promises a number for money:\n" + "\n".join(offenders)


def test_raw_account_numbers_never_leave_the_server(client: TestClient) -> None:
    """The fixture's real-shaped keys must not appear in any response."""
    raw_keys = {
        line.split(",")[0]
        for line in FIXTURE.read_text(encoding="utf-8-sig").splitlines()[1:]
        if line and "," in line and line.split(",")[0].strip()
    }
    raw_keys = {k for k in raw_keys if len(k) > 6}
    assert raw_keys, "the fixture should contain account keys"

    body = json.dumps(client.get("/api/v1/accounts").json())
    leaked = [key for key in raw_keys if key in body]
    assert not leaked, f"raw account keys leaked into the API: {leaked[:3]}"

    for account in client.get("/api/v1/accounts").json():
        assert account["account_key_masked"].startswith("*")


# --- content --------------------------------------------------------------


def test_summary_carries_its_caveats(client: TestClient) -> None:
    """A total is never served without what qualifies it."""
    body = client.get("/api/v1/portfolio/summary").json()
    assert Decimal(body["total_value"]) > 0
    assert body["account_count"] == 10
    assert body["position_count"] == 80
    assert body["quality"]["is_fully_trusted"] is False
    assert any("as-of date is unknown" in c for c in body["quality"]["caveats"])
    assert body["skipped_account_count"] == 4


def test_tax_allocation_reports_what_it_excluded(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio/allocation/tax-treatment").json()
    assert Decimal(body["excluded_value"]) > 0
    assert "no confirmed tax treatment" in body["excluded_label"]
    assert "unknown" not in {s["key"] for s in body["slices"]}


def test_cash_endpoint_counts_more_than_the_summarys_cash_value(client: TestClient) -> None:
    """`cash_value` is settlement balances only; a money-market fund is a holding.

    The dashboard showed the first and called it "Cash", which understated real
    cash by every sweep and T-bill in the portfolio.
    """
    body = client.get("/api/v1/portfolio/cash").json()
    summary = client.get("/api/v1/portfolio/summary").json()
    assert Decimal(body["settlement_value"]) == Decimal(summary["cash_value"])
    assert Decimal(body["holdings_value"]) > 0
    assert Decimal(body["total_value"]) == Decimal(body["settlement_value"]) + Decimal(
        body["holdings_value"]
    )


def test_cash_buckets_partition_the_total(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio/cash").json()
    assert body["buckets"], "the fixture portfolio holds cash equivalents"
    assert sum(Decimal(b["total_value"]) for b in body["buckets"]) == Decimal(body["total_value"])
    assert set(b["key"] for b in body["buckets"]) <= {"taxable", "tax_sheltered", "unknown"}
    for bucket in body["buckets"]:
        assert Decimal(bucket["total_value"]) == Decimal(bucket["settlement_value"]) + Decimal(
            bucket["holdings_value"]
        )


def test_cash_of_unknown_tax_treatment_is_not_counted_as_taxable(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio/cash").json()
    unknown = next((b for b in body["buckets"] if b["key"] == "unknown"), None)
    if unknown is None:
        pytest.skip("the fixture has no cash in an unclassified account")
    assert Decimal(body["unconfirmed_value"]) == Decimal(unknown["total_value"])
    taxable = next((b for b in body["buckets"] if b["key"] == "taxable"), None)
    if taxable is not None:
        assert Decimal(taxable["total_value"]) == Decimal(body["taxable_value"])


def test_gains_report_partial_coverage(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio/gains").json()
    assert body["coverage"]["caveat"] is not None
    assert Decimal(body["coverage"]["covered_fraction"]) < 1


def test_security_exposure_never_exposes_a_summed_quantity(client: TestClient) -> None:
    """Shares from different share bases must not be added."""
    body = client.get("/api/v1/portfolio/securities").json()
    multi = [e for e in body if e["account_count"] > 1]
    assert multi
    for exposure in multi:
        assert "total_quantity" not in exposure
        assert len(exposure["holdings"]) == exposure["account_count"]


def test_divergent_price_filter_returns_the_real_cases(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio/securities?divergent_only=true").json()
    assert len(body) >= 6
    assert all(e["has_divergent_prices"] for e in body)


def test_closed_holdings_are_hidden_unless_asked_for(client: TestClient) -> None:
    default = client.get("/api/v1/holdings").json()
    included = client.get("/api/v1/holdings?include_closed=true").json()
    assert len(included) > len(default)
    assert not any(h["is_closed"] for h in default)


def test_holdings_can_be_filtered(client: TestClient) -> None:
    everything = client.get("/api/v1/holdings").json()
    account_id = everything[0]["account_id"]
    filtered = client.get(f"/api/v1/holdings?account_id={account_id}").json()
    assert filtered
    assert all(h["account_id"] == account_id for h in filtered)


# --- the classification workflow -----------------------------------------


def test_accounts_needing_classification_are_listed(client: TestClient) -> None:
    """The Account Setup worklist."""
    body = client.get("/api/v1/accounts/needs-classification").json()
    assert body
    assert all(a["needs_classification"] for a in body)


def test_confirming_a_classification_sticks_and_is_audited(
    client: TestClient, session: Session
) -> None:
    from portfolio_manager.models.entities import AccountAuditLog

    target = client.get("/api/v1/accounts/needs-classification").json()[0]
    response = client.post(
        f"/api/v1/accounts/{target['id']}/classification",
        json={
            "account_type": "brokerage",
            "tax_treatment": "taxable",
            "reason": "confirmed from the statement",
        },
    )
    assert response.status_code == 200
    assert response.json()["tax_treatment"] == "taxable"
    assert response.json()["needs_classification"] is False

    logged = session.query(AccountAuditLog).filter_by(account_id=target["id"]).all()
    assert any(entry.field == "tax_treatment" for entry in logged)
    assert all(entry.changed_by == "user" for entry in logged)


def test_choosing_a_preferred_source_is_recorded(client: TestClient) -> None:
    account = client.get("/api/v1/accounts").json()[0]
    response = client.post(
        f"/api/v1/accounts/{account['id']}/preferred-source",
        json={"institution": "Vanguard", "reason": "direct statement is authoritative"},
    )
    assert response.status_code == 200
    assert response.json()["preferred_source"] == "Vanguard"


def test_unknown_account_is_a_404(client: TestClient) -> None:
    import uuid

    missing = str(uuid.uuid4())
    assert client.get(f"/api/v1/accounts/{missing}/holdings").status_code == 404


# --- snapshot visibility on the value-over-time chart --------------------


def test_snapshots_start_shown_on_the_chart(client: TestClient) -> None:
    snapshots = client.get("/api/v1/snapshots").json()
    assert snapshots and all(s["hidden_from_history"] is False for s in snapshots)


def test_hiding_a_snapshot_round_trips(client: TestClient) -> None:
    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]

    hidden = client.patch(f"/api/v1/snapshots/{snapshot_id}", json={"hidden_from_history": True})
    assert hidden.status_code == 200
    assert hidden.json()["hidden_from_history"] is True
    assert client.get("/api/v1/snapshots").json()[0]["hidden_from_history"] is True

    shown = client.patch(f"/api/v1/snapshots/{snapshot_id}", json={"hidden_from_history": False})
    assert shown.json()["hidden_from_history"] is False


def test_hiding_only_affects_the_chart(client: TestClient) -> None:
    """A hidden snapshot is still the current portfolio, and still counts."""
    before = client.get("/api/v1/portfolio/summary").json()
    client.patch(f"/api/v1/snapshots/{before['snapshot_id']}", json={"hidden_from_history": True})
    after = client.get("/api/v1/portfolio/summary").json()
    assert after["snapshot_id"] == before["snapshot_id"]
    assert after["total_value"] == before["total_value"]


def test_hiding_an_unknown_snapshot_is_a_404(client: TestClient) -> None:
    import uuid

    response = client.patch(f"/api/v1/snapshots/{uuid.uuid4()}", json={"hidden_from_history": True})
    assert response.status_code == 404


def test_visibility_must_be_stated_explicitly(client: TestClient) -> None:
    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]
    assert client.patch(f"/api/v1/snapshots/{snapshot_id}", json={}).status_code == 422


# --- imports --------------------------------------------------------------


def test_uploading_the_same_file_again_is_a_no_op(client: TestClient) -> None:
    response = client.post(
        "/api/v1/imports",
        files={"file": ("fullview.csv", FIXTURE.read_bytes(), "text/csv")},
        params={"provider": "mock"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "duplicate"
    assert "already imported" in body["message"]


def test_an_empty_upload_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/imports", files={"file": ("empty.csv", b"", "text/csv")})
    assert response.status_code == 400


def test_an_unreadable_document_reports_why(client: TestClient) -> None:
    """Never "AI parsing failed" when a concrete reason exists."""
    response = client.post(
        "/api/v1/imports",
        files={"file": ("junk.csv", b"alpha,beta\n1,2\n", "text/csv")},
        params={"provider": "mock"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unsupported_document"
    assert "account" in detail["message"]


# --- deleting a snapshot -------------------------------------------------


def test_the_removal_preview_says_what_would_go_and_changes_nothing(
    client: TestClient,
) -> None:
    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]
    preview = client.get(f"/api/v1/snapshots/{snapshot_id}/removal").json()

    assert preview["snapshot"]["id"] == snapshot_id
    assert preview["account_count"] > 0 and preview["position_count"] > 0
    assert preview["documents"], "the document becomes importable again"
    assert preview["needs_force"] is False and preview["objections"] == []
    assert len(client.get("/api/v1/snapshots").json()) == 1, "a preview deletes nothing"


def test_deleting_a_snapshot_lets_its_document_be_imported_again(client: TestClient) -> None:
    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]

    assert client.delete(f"/api/v1/snapshots/{snapshot_id}").status_code == 204
    assert client.get("/api/v1/snapshots").json() == []

    again = client.post(
        "/api/v1/imports",
        files={"file": ("fullview.csv", FIXTURE.read_bytes(), "text/csv")},
        params={"provider": "mock"},
    )
    assert again.json()["outcome"] == "imported", "no longer a duplicate"


def test_deleting_a_snapshot_the_journal_cites_needs_force(
    client: TestClient, session: Session
) -> None:
    """A journal entry would silently lose the portfolio it was written against."""
    from portfolio_manager.models.advisory import Recommendation

    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]
    session.add(
        Recommendation(portfolio_snapshot_id=snapshot_id, title="a fixture", rationale="not advice")
    )
    session.flush()

    preview = client.get(f"/api/v1/snapshots/{snapshot_id}/removal").json()
    assert preview["needs_force"] is True
    refused = client.delete(f"/api/v1/snapshots/{snapshot_id}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["objections"] == preview["objections"]
    assert len(client.get("/api/v1/snapshots").json()) == 1, "nothing was deleted"

    forced = client.delete(f"/api/v1/snapshots/{snapshot_id}", params={"force": True})
    assert forced.status_code == 204
    assert client.get("/api/v1/snapshots").json() == []


def test_deleting_an_unknown_snapshot_is_a_404(client: TestClient) -> None:
    import uuid

    missing = uuid.uuid4()
    assert client.delete(f"/api/v1/snapshots/{missing}").status_code == 404
    assert client.get(f"/api/v1/snapshots/{missing}/removal").status_code == 404


# --- importing a pair at once --------------------------------------------

VANGUARD_SHAPED = (
    Path(__file__).resolve().parents[1]
    / "fixtures/extraction/vanguard/direct_source_anonymized.json"
)
PDF_BYTES = b"%PDF-1.4\n% a placeholder the mock never parses\n"


@pytest.fixture
def empty_client(session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """No portfolio yet, and a mock that can read the direct-source PDF.

    The direct source states a date a day after the aggregation, as the real
    exports do -- the case that opens a second, partial snapshot when the two
    are uploaded one at a time.
    """
    from portfolio_manager.api.v1 import imports
    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument
    from portfolio_manager.extraction.contracts import SourceDocument

    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    provider = MockExtractionProvider()
    document = ExtractedPortfolioDocument.model_validate(json.loads(VANGUARD_SHAPED.read_text()))
    document = document.model_copy(update={"as_of_date": "08/27/2026"})
    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(PDF_BYTES)
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, document)
    monkeypatch.setattr(imports, "get_provider", lambda name, settings: provider)

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _pair(pdf_first: bool = False) -> list[tuple[str, tuple[str, bytes, str]]]:
    csv = ("file", ("fullview.csv", FIXTURE.read_bytes(), "text/csv"))
    pdf = ("file", ("direct.pdf", PDF_BYTES, "application/pdf"))
    ordered = [pdf, csv] if pdf_first else [csv, pdf]
    return [("files", entry[1]) for entry in ordered]


def test_a_pair_uploaded_together_is_one_portfolio(empty_client: TestClient) -> None:
    response = empty_client.post("/api/v1/imports/batch", files=_pair())
    assert response.status_code == 201
    body = response.json()

    assert {r["outcome"] for r in body["results"]} == {"imported"}
    assert len({r["snapshot_id"] for r in body["results"]}) == 1, "one snapshot, not two"
    assert [r["joined"] for r in body["results"]] == [False, True]
    assert len(empty_client.get("/api/v1/snapshots").json()) == 1


def test_a_pair_is_filed_widest_first_whatever_order_it_was_sent(
    empty_client: TestClient,
) -> None:
    body = empty_client.post("/api/v1/imports/batch", files=_pair(pdf_first=True)).json()

    assert body["reordered"] is True
    assert body["order"] == ["fullview.csv", "direct.pdf"]
    assert all(not r["unmatched_accounts"] for r in body["results"]), "nothing left behind"
    assert empty_client.get("/api/v1/portfolio/summary").json()["account_count"] == 10


def test_results_carry_the_users_filenames(empty_client: TestClient) -> None:
    body = empty_client.post("/api/v1/imports/batch", files=_pair()).json()
    assert sorted(r["filename"] for r in body["results"]) == ["direct.pdf", "fullview.csv"]


def test_a_document_can_join_an_existing_snapshot_afterwards(empty_client: TestClient) -> None:
    """The direct source arrives after the aggregation was already filed."""
    csv = [("files", ("fullview.csv", FIXTURE.read_bytes(), "text/csv"))]
    first = empty_client.post("/api/v1/imports/batch", files=csv).json()
    as_of = first["as_of_date"]

    pdf = [("files", ("direct.pdf", PDF_BYTES, "application/pdf"))]
    joined = empty_client.post("/api/v1/imports/batch", files=pdf, params={"as_of": as_of})
    assert joined.status_code == 201
    result = joined.json()["results"][0]
    assert result["joined"] is True
    assert result["snapshot_id"] == first["results"][0]["snapshot_id"]
    assert len(empty_client.get("/api/v1/snapshots").json()) == 1, "joined, not a second one"


def test_joining_a_snapshot_dated_far_away_needs_confirming(empty_client: TestClient) -> None:
    """A month-old statement would reconcile perfectly against itself."""
    empty_client.post(
        "/api/v1/imports/batch",
        files=[("files", ("fullview.csv", FIXTURE.read_bytes(), "text/csv"))],
    )
    pdf = [("files", ("direct.pdf", PDF_BYTES, "application/pdf"))]
    far = "2026-12-31"

    refused = empty_client.post("/api/v1/imports/batch", files=pdf, params={"as_of": far})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["kind"] == "date_override_refused"
    assert detail["requested"] == far and detail["tolerance_days"] > 0

    forced = empty_client.post(
        "/api/v1/imports/batch", files=pdf, params={"as_of": far, "force_as_of": True}
    )
    assert forced.status_code == 201
    assert forced.json()["as_of_date"] == far


def test_a_batch_with_an_empty_file_is_refused_before_anything_is_filed(
    empty_client: TestClient,
) -> None:
    files = [
        ("files", ("fullview.csv", FIXTURE.read_bytes(), "text/csv")),
        ("files", ("empty.pdf", b"", "application/pdf")),
    ]
    response = empty_client.post("/api/v1/imports/batch", files=files)
    assert response.status_code == 400
    assert "empty.pdf" in response.json()["detail"]
    assert empty_client.get("/api/v1/snapshots").json() == []


def test_a_batch_refuses_two_files_with_one_name(empty_client: TestClient) -> None:
    files = [
        ("files", ("same.csv", FIXTURE.read_bytes(), "text/csv")),
        ("files", ("same.csv", b"other", "text/csv")),
    ]
    assert empty_client.post("/api/v1/imports/batch", files=files).status_code == 400


def test_a_one_file_batch_is_an_ordinary_import(client: TestClient) -> None:
    files = [("files", ("fullview.csv", FIXTURE.read_bytes(), "text/csv"))]
    body = client.post("/api/v1/imports/batch", files=files, params={"provider": "mock"}).json()
    assert [r["outcome"] for r in body["results"]] == ["duplicate"]
    assert body["reordered"] is False


# --- ops ------------------------------------------------------------------


def test_health_reports_the_active_provider(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


def test_openapi_documents_the_money_convention(client: TestClient) -> None:
    description = client.get("/openapi.json").json()["info"]["description"]
    assert "Money is a string" in description
    assert "masked" in description


# --- investment policy ----------------------------------------------------


def test_policy_is_absent_until_set(client: TestClient) -> None:
    response = client.get("/api/v1/policy")
    assert response.status_code == 404
    assert "starter" in response.json()["detail"]


def test_the_starter_policy_is_valid_yaml_that_can_be_saved(client: TestClient) -> None:
    starter = client.get("/api/v1/policy/starter")
    assert starter.status_code == 200
    assert "text/yaml" in starter.headers["content-type"]

    saved = client.put("/api/v1/policy", json={"yaml": starter.text})
    assert saved.status_code == 200
    assert saved.json()["version"] == 1


def test_an_inconsistent_policy_is_refused_with_a_reason(client: TestClient) -> None:
    """A policy that does not add up would distort every drift figure."""
    response = client.put(
        "/api/v1/policy",
        json={"yaml": "targets:\n  us_equity: 0.6\n  fixed_income: 0.2\n"},
    )
    assert response.status_code == 422
    assert "sum to 1.0" in response.json()["detail"]


def test_saving_creates_a_new_version_rather_than_overwriting(client: TestClient) -> None:
    """'What was my policy when I made that decision?' must stay answerable."""
    first = "targets:\n  us_equity: 1.0\ndrift_band: 0.05\n"
    second = "targets:\n  us_equity: 0.6\n  fixed_income: 0.4\ndrift_band: 0.05\n"
    assert client.put("/api/v1/policy", json={"yaml": first}).json()["version"] == 1
    assert client.put("/api/v1/policy", json={"yaml": second}).json()["version"] == 2

    versions = client.get("/api/v1/policy/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    assert [v["is_active"] for v in versions] == [True, False]


def test_the_users_own_yaml_is_preserved_verbatim(client: TestClient) -> None:
    """Comments and ordering are what survive review; regenerating loses them."""
    text = (
        "# Why these targets: I want a simple two-fund portfolio.\n"
        "targets:\n"
        "  us_equity: 0.7   # home bias, deliberate\n"
        "  fixed_income: 0.3\n"
        "drift_band: 0.05\n"
    )
    client.put("/api/v1/policy", json={"yaml": text})
    stored = client.get("/api/v1/policy/yaml").text
    assert "# Why these targets" in stored
    assert "home bias, deliberate" in stored


def test_assessment_requires_a_policy(client: TestClient) -> None:
    response = client.get("/api/v1/policy/assessment")
    assert response.status_code == 404
    assert "nothing to assess" in response.json()["detail"]


def test_assessment_reports_drift_and_its_caveats(client: TestClient) -> None:
    client.put(
        "/api/v1/policy",
        json={
            "yaml": (
                "targets:\n  us_equity: 0.5\n  intl_equity: 0.2\n"
                "  fixed_income: 0.25\n  cash_equivalent: 0.05\n"
                "drift_band: 0.05\n"
                "constraints:\n"
                "  - id: cap\n    kind: max_single_holding\n    limit: 0.10\n"
            )
        },
    )
    body = client.get("/api/v1/policy/assessment").json()

    assert body["policy_version"] == 1
    assert body["drift"], "every target should be evaluated"
    assert {d["direction"] for d in body["drift"]} <= {"in_band", "overweight", "underweight"}
    # The real corpus is stale and partly unclassified, so this must not be
    # presented as ready to act on.
    assert body["is_provisional"] is True
    assert body["caveats"]

    for field in ("target", "actual", "drift", "rebalance_value"):
        assert isinstance(body["drift"][0][field], str), f"{field} must be a string"


def test_assessment_flags_a_constraint_breach_with_the_actual_number(
    client: TestClient,
) -> None:
    client.put(
        "/api/v1/policy",
        json={
            "yaml": (
                "targets:\n  us_equity: 1.0\ndrift_band: 0.5\n"
                "constraints:\n"
                "  - id: cap\n    kind: max_single_holding\n    limit: 0.01\n"
                "    description: No single security above 1%.\n"
            )
        },
    )
    body = client.get("/api/v1/policy/assessment").json()
    assert body["breaches"], "a 1% cap must be breached by this portfolio"
    breach = body["breaches"][0]
    assert breach["id"] == "cap"
    assert breach["satisfied"] is False
    assert Decimal(breach["actual"]) > Decimal("0.01")
    assert "limit" in breach["detail"]


# --- recommendation journal -----------------------------------------------


def test_the_journal_starts_empty(client: TestClient) -> None:
    assert client.get("/api/v1/recommendations").json() == []
    stats = client.get("/api/v1/recommendations/stats").json()
    assert stats["total_recommendations"] == 0
    assert stats["acceptance_rate"] is None


def test_a_user_can_record_their_own_reasoning(client: TestClient) -> None:
    """The journal is not only for the analyst."""
    response = client.post(
        "/api/v1/recommendations",
        json={
            "title": "Hold through the drawdown",
            "rationale": "Selling here would realise a gain I do not need to.",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "user"
    assert body["status"] == "pending"
    assert body["is_open"] is True


def test_a_decision_appends_and_the_history_is_returned(client: TestClient) -> None:
    created = client.post("/api/v1/recommendations", json={"title": "t", "rationale": "r"}).json()

    deferred = client.post(
        f"/api/v1/recommendations/{created['id']}/decision",
        json={"decision": "deferred", "note": "revisit after April"},
    ).json()
    assert deferred["status"] == "deferred"
    assert deferred["is_open"] is True, "deferring is not being finished with it"

    accepted = client.post(
        f"/api/v1/recommendations/{created['id']}/decision",
        json={"decision": "accepted", "note": "done"},
    ).json()
    assert accepted["status"] == "accepted"
    assert accepted["is_open"] is False
    assert [d["decision"] for d in accepted["decisions"]] == ["deferred", "accepted"]
    assert accepted["decisions"][0]["note"] == "revisit after April"


def test_open_recommendations_include_deferred_ones(client: TestClient) -> None:
    pending = client.post(
        "/api/v1/recommendations", json={"title": "pending", "rationale": "r"}
    ).json()
    deferred = client.post(
        "/api/v1/recommendations", json={"title": "deferred", "rationale": "r"}
    ).json()
    closed = client.post(
        "/api/v1/recommendations", json={"title": "closed", "rationale": "r"}
    ).json()
    client.post(f"/api/v1/recommendations/{deferred['id']}/decision", json={"decision": "deferred"})
    client.post(f"/api/v1/recommendations/{closed['id']}/decision", json={"decision": "rejected"})

    titles = {r["title"] for r in client.get("/api/v1/recommendations/open").json()}
    assert titles == {"pending", "deferred"}
    assert pending["id"]


def test_stats_report_the_acceptance_rate_of_decided_items(client: TestClient) -> None:
    for title, decision in (("a", "accepted"), ("b", "rejected"), ("c", "deferred")):
        created = client.post(
            "/api/v1/recommendations", json={"title": title, "rationale": "r"}
        ).json()
        client.post(
            f"/api/v1/recommendations/{created['id']}/decision", json={"decision": decision}
        )

    stats = client.get("/api/v1/recommendations/stats").json()
    assert stats["decided"] == 2, "a deferred item is not decided"
    assert Decimal(stats["acceptance_rate"]) == Decimal("0.5")


def test_an_invalid_decision_is_rejected(client: TestClient) -> None:
    created = client.post("/api/v1/recommendations", json={"title": "t", "rationale": "r"}).json()
    response = client.post(
        f"/api/v1/recommendations/{created['id']}/decision",
        json={"decision": "obliterated"},
    )
    assert response.status_code == 422


def test_deciding_on_a_missing_recommendation_is_a_404(client: TestClient) -> None:
    import uuid

    response = client.post(
        f"/api/v1/recommendations/{uuid.uuid4()}/decision", json={"decision": "accepted"}
    )
    assert response.status_code == 404


@pytest.mark.skipif(
    not get_settings().frontend_dir.is_dir(),
    reason="the frontend has not been built; run make ui-build",
)
def test_a_client_route_survives_a_reload(client: TestClient) -> None:
    """The router lives in the browser, so `/holdings` is a page but not a file.

    Plain StaticFiles 404s it, which means the app works while you click through
    it and breaks the moment you reload, bookmark, or follow a link straight to
    a page. Found by opening a deep link rather than by any test.
    """
    for route in ("holdings", "accounts", "imports", "policy", "journal"):
        response = client.get(f"/{route}")
        assert response.status_code == 200, f"/{route} should serve the app"
        assert response.text.lstrip().lower().startswith("<!doctype html>")


@pytest.mark.skipif(
    not get_settings().frontend_dir.is_dir(),
    reason="the frontend has not been built; run make ui-build",
)
def test_the_fallback_does_not_swallow_a_missing_api_route(client: TestClient) -> None:
    """A mistyped endpoint must answer 404, not 200 and a web page.

    The first version of the fallback did exactly that: every unmatched path
    became the app, so an API client parsing the response got a syntax error
    instead of a status it could act on.
    """
    assert client.get("/api/v1/nope").status_code == 404
    assert client.get("/api/v1/accounts/nope").status_code == 404
    # A missing asset stays a miss too; HTML in place of a script is a blank
    # screen rather than a clear error.
    assert client.get("/assets/nope-123.js").status_code == 404
    # And the server's own paths still answer.
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# --- advisor inputs -----------------------------------------------------------


def test_philosophy_is_absent_until_written_then_versioned(client: TestClient) -> None:
    assert client.get("/api/v1/advisor/philosophy").status_code == 404
    starter = client.get("/api/v1/advisor/philosophy/starter")
    assert starter.status_code == 200 and "## Risk" in starter.text

    first = client.put("/api/v1/advisor/philosophy", json={"body_markdown": "Index funds."})
    second = client.put("/api/v1/advisor/philosophy", json={"body_markdown": "Index + value."})
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    assert client.get("/api/v1/advisor/philosophy").json()["body_markdown"] == "Index + value."
    history = client.get("/api/v1/advisor/philosophy/history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert client.put("/api/v1/advisor/philosophy", json={"body_markdown": " "}).status_code == 422


def test_context_notes_urls_and_documents_round_trip(client: TestClient) -> None:
    note = client.post(
        "/api/v1/advisor/context/notes",
        json={"kind": "view", "title": "Rates", "body": "Rates fall", "expires_on": "2000-01-01"},
    )
    assert note.status_code == 201
    url = client.post(
        "/api/v1/advisor/context/urls", json={"url": "https://example.com/a", "note": "read"}
    )
    assert url.json()["extraction"] == "not_captured"
    assert (
        client.post("/api/v1/advisor/context/urls", json={"url": "file:///etc/passwd"}).status_code
        == 422
    )
    doc = client.post(
        "/api/v1/advisor/context/documents",
        files={"file": ("views.md", b"# Views\nPrefer short duration bonds.", "text/markdown")},
        data={"note": "my notes"},
    )
    assert doc.status_code == 201, doc.text
    assert doc.json()["extraction"] == "extracted"

    listing = client.get("/api/v1/advisor/context").json()
    assert [i["title"] for i in listing["expired"]] == ["Rates"]
    assert listing["expired"][0]["is_expired"] is True
    assert {i["kind"] for i in listing["current"]} == {"url", "document"}

    patched = client.patch(
        f"/api/v1/advisor/context/{note.json()['id']}", json={"clear_expiry": True}
    )
    assert patched.json()["expires_on"] is None
    assert client.delete(f"/api/v1/advisor/context/{doc.json()['id']}").status_code == 204
    assert client.patch("/api/v1/advisor/context/not-a-uuid", json={}).status_code == 404
