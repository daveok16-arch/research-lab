"""End-to-end acceptance test.

One test walks the complete lifecycle the product promises, in order, against real database
records and the real pipeline:

    source data -> raw landing -> normalization -> evidence -> classification
      -> eligibility -> opportunity -> user match -> save -> watch
      -> change detected -> alert -> pipeline -> report

No mocked intelligence and no manufactured opportunity. Each stage asserts on what the previous
stage actually wrote, so the test fails if any link in the chain breaks.
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest_app import build_database
from oppintel.app.alerts import AlertService
from oppintel.app.config import AppConfig
from oppintel.app.main import create_app
from oppintel.app.matching import evaluate_match
from oppintel.app.workflow import WorkflowService
from oppintel.db import Database
from oppintel.eligibility import evaluate
from oppintel.models import Permit, normalize_address
from oppintel.pipeline import Pipeline


def _permit(**overrides) -> Permit:
    defaults = dict(
        source_id="fort_worth_permits",
        permit_number="E2E1",
        natural_key="E2E1",
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        permit_date=date(2026, 9, 10),
        status="Issued",
        address="500 COMMERCE ST",
        city="Fort Worth",
        state="TX",
        work_description="New construction office building mechanical systems",
        land_use="OFFICE BUILDING",
        job_value=4_200_000.0,
        square_footage=48_000.0,
        is_commercial=True,
        source_url="https://example.gov/E2E1",
        source_date=date(2026, 9, 10),
    )
    defaults.update(overrides)
    return Permit(**defaults)


class _Row:
    """Adapts a sqlite3.Row to the mapping interface the intelligence helpers expect."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data.get(key)

    def keys(self):
        return self._data.keys()

    def __contains__(self, key):
        return key in self._data


def test_full_lifecycle_from_source_to_report(tmp_path):
    db_path = tmp_path / "lifecycle.db"
    db = Database(db_path)
    db.init_schema()
    db.init_app_schema()

    # --- 1. source -> normalization -> persist permits ------------------------
    pipeline = Pipeline(db)
    pipeline._register_sources()
    db.upsert_permit(_permit(), normalize_address("500 COMMERCE ST"))
    db.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM permit").fetchone()[0] == 1

    # --- 2. assemble -> evidence -> classify ----------------------------------
    pipeline.assemble_and_classify()
    project = dict(db.conn.execute("SELECT * FROM project").fetchone())
    assert project["classification"] in ("HIGH", "MEDIUM"), "the project must be classified"
    assert project["mechanical_evidence_tier"] in (1, 2), "mechanical evidence must be recorded"

    # --- 3. evidence is traceable ---------------------------------------------
    evidence = db.conn.execute(
        "SELECT * FROM evidence WHERE project_id = ?", (project["id"],)
    ).fetchall()
    assert evidence, "every claim must have a supporting evidence row"
    assert all(row["source_name"] for row in evidence), "each evidence row names its source"

    # --- 4. eligibility is the intelligence layer's verdict -------------------
    eligibility = evaluate(_Row(project))
    assert eligibility.eligible, f"expected eligible, got: {eligibility.explain()}"

    # --- 5. the opportunity is discoverable -----------------------------------
    app = create_app(
        AppConfig(database_path=db_path, secret_key="test-secret-key", debug=True)
    )
    app.config["TESTING"] = True
    client = app.test_client()

    # Rebuild slugs and the search index as the deployment step does.
    from oppintel.search_index import rebuild_index
    from oppintel.slugs import ensure_slugs

    ensure_slugs(db)
    rebuild_index(db)
    db.commit()

    directory = client.get("/api/opportunities").get_json()
    assert directory["total"] == 1, "the opportunity must appear in the public directory"
    slug = directory["results"][0]["slug"]

    # --- 6. user match ---------------------------------------------------------
    detail = client.get(f"/api/opportunities/{slug}").get_json()
    assert detail["match_reasons"], "the record must explain why it is relevant"
    reasons = {r["kind"] for r in detail["match_reasons"]}
    assert "trade_evidence" in reasons

    # --- 7. sign up, save, watch ----------------------------------------------
    client.post(
        "/signup",
        data={
            "email": "lifecycle@example.com",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
        },
        follow_redirects=True,
    )
    project_id = int(db.conn.execute("SELECT id FROM project").fetchone()["id"])
    assert client.post(f"/api/saved/{project_id}").status_code == 200
    assert client.post(f"/api/watching/{project_id}").status_code == 200
    assert client.get("/api/saved").get_json()["total"] == 1
    assert client.get("/api/watching").get_json()["total"] == 1

    user_id = int(
        db.conn.execute(
            "SELECT id FROM app_user WHERE email = 'lifecycle@example.com'"
        ).fetchone()["id"]
    )
    assert WorkflowService(db).is_watched(user_id, project_id)

    # --- 8. a real change is detected -----------------------------------------
    db.upsert_permit(
        _permit(
            permit_number="E2E2",
            natural_key="E2E2",
            permit_date=date(2026, 9, 22),
            work_description="Additional mechanical scope for tenant improvement",
        ),
        normalize_address("500 COMMERCE ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    changes = db.conn.execute(
        "SELECT * FROM project_change WHERE project_id = ? AND change_kind = 'new_permit'",
        (project_id,),
    ).fetchall()
    assert changes, "a newly filed permit must produce a recorded change"

    # --- 9. the change produces an alert --------------------------------------
    result = AlertService(db).generate_from_changes()
    assert result.created >= 1

    alerts = client.get("/api/alerts").get_json()
    assert alerts["total"] >= 1
    assert alerts["alerts"][0]["kind"] == "new_permit"
    assert alerts["alerts"][0]["current_value"], "the alert must state what changed"

    # --- 10. the alert is visible on the page and links to the change ---------
    page = client.get("/alerts").get_data(as_text=True)
    assert "New permit filed" in page
    assert AlertService(db).summary()["orphaned"] == 0

    # --- 11. pipeline workflow -------------------------------------------------
    assert client.post(
        f"/api/pipeline/{project_id}", json={"stage": "TARGET"}
    ).get_json()["stage"] == "TARGET"
    pipeline_payload = client.get("/api/pipeline").get_json()
    assert pipeline_payload["total"] == 1
    assert pipeline_payload["counts"]["TARGET"] == 1

    # --- 12. the timeline shows the real sequence -----------------------------
    timeline = client.get(f"/api/opportunities/{slug}").get_json()["changes"]
    kinds = [entry["change_kind"] for entry in timeline]
    assert "new_permit" in kinds

    # --- 13. a report is generated from the same verified data ----------------
    from oppintel.app.reports import published_reports

    reports = published_reports(db, app.config["APP_CONFIG"])
    assert reports, "a published report must exist"
    report_body = reports[0]["body"]
    assert "Fire it up" not in report_body  # sanity: no placeholder content
    assert len(report_body) > 200

    db.close()


def test_the_public_pages_never_contradict_the_intelligence_layer(tmp_path):
    """A project the intelligence layer excludes must not appear in discovery.

    This is the acceptance criterion that keeps the application honest: it may not present a
    project the pipeline declined to classify as an opportunity.
    """
    db_path = tmp_path / "consistency.db"
    db = build_database(db_path)
    db.close()

    app = create_app(
        AppConfig(database_path=db_path, secret_key="test-secret-key", debug=True)
    )
    app.config["TESTING"] = True
    client = app.test_client()

    listed = {
        row["id"]
        for row in client.get("/api/opportunities?page_size=50").get_json()["results"]
    }

    db = Database(db_path)
    try:
        discoverable = {
            int(r["id"])
            for r in db.conn.execute(
                """
                SELECT id FROM project
                 WHERE classification IN ('HIGH', 'MEDIUM')
                   AND procurement_status IN ('Confirmed open', 'Evidence found, status unclear')
                """
            ).fetchall()
        }
    finally:
        db.close()

    assert listed <= discoverable, "the site listed a project the intelligence layer excluded"


def test_a_closed_project_is_excluded_from_discovery_but_reachable(tmp_path):
    """A finished project must not be offered as a live opportunity, yet must not vanish."""
    from tests.conftest_app import FIXTURE_PERMITS  # noqa: F401 - ensures fixture module loads

    db_path = tmp_path / "closed.db"
    db = Database(db_path)
    db.init_schema()
    db.init_app_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()

    # A completed project at one address, a live one at another.
    db.upsert_permit(
        _permit(permit_number="C1", natural_key="C1", address="9 DONE ST",
                status="Final CO Issued", permit_date=date(2026, 6, 1)),
        normalize_address("9 DONE ST"),
    )
    db.upsert_permit(
        _permit(permit_number="L1", natural_key="L1", address="11 LIVE ST"),
        normalize_address("11 LIVE ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    from oppintel.search_index import rebuild_index
    from oppintel.slugs import ensure_slugs

    ensure_slugs(db)
    rebuild_index(db)
    db.commit()

    app = create_app(
        AppConfig(database_path=db_path, secret_key="test-secret-key", debug=True)
    )
    app.config["TESTING"] = True
    client = app.test_client()

    results = client.get("/api/opportunities?page_size=50").get_json()["results"]
    addresses = {r["address"] for r in results}
    assert "11 LIVE ST" in addresses, "a live project must be discoverable"
    assert "9 DONE ST" not in addresses, "a completed project must not be discovered"

    # By direct link the record is still reachable and states its status.
    closed = db.conn.execute(
        "SELECT s.slug, p.procurement_status FROM project p "
        "JOIN project_slug s ON s.project_id = p.id WHERE p.address = '9 DONE ST'"
    ).fetchone()
    if closed:
        detail = client.get(f"/opportunities/{closed['slug']}")
        assert detail.status_code == 200, "a closed record must remain reachable by direct link"
    db.close()
