"""Shared fixtures for application tests.

The application tests run against a database built from synthetic permits, so they exercise
the real service, eligibility and rendering paths without depending on the live dataset. The
intelligence tests continue to run against real captured source payloads; these tests cover the
web layer, where a fixture dataset is the right tool because the assertions are about routing,
filtering and rendering rather than about parsing a real government feed.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from oppintel.app.config import AppConfig  # noqa: E402
from oppintel.app.main import create_app  # noqa: E402
from oppintel.db import Database  # noqa: E402
from oppintel.models import Permit, normalize_address  # noqa: E402
from oppintel.pipeline import Pipeline  # noqa: E402
from oppintel.search_index import rebuild_index  # noqa: E402
from oppintel.slugs import ensure_slugs  # noqa: E402


def _permit(**overrides) -> Permit:
    defaults = dict(
        source_id="fort_worth_permits",
        permit_number="PB1",
        natural_key="PB1",
        permit_type="Commercial Building Permit",
        permit_subtype="New",
        permit_date=date(2026, 9, 1),
        status="Issued",
        address="100 MAIN ST",
        city="Fort Worth",
        state="TX",
        work_description="New construction of medical office building",
        land_use="OFFICE BUILDING",
        job_value=5_000_000.0,
        is_commercial=True,
        source_url="https://example.gov/PB1",
        source_date=date(2026, 9, 1),
    )
    defaults.update(overrides)
    return Permit(**defaults)


#: The fixture dataset. Deliberately mirrors the real shapes that matter:
#: a mechanical project, a completed project, a service-only permit, a plumbing-only record,
#: a suite pair sharing one building, and a record with no mechanical evidence.
FIXTURE_PERMITS: list[dict] = [
    # 1. Eligible high-context project: mechanical permit plus a building class and value.
    dict(permit_number="M1", natural_key="M1", address="10 ROSS AVE",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Mechanical remodel of spec suite", land_use="",
         job_value=2_500_000.0, permit_date=date(2026, 9, 10)),
    # 2. Completed work: must be excluded from discovery.
    dict(permit_number="C1", natural_key="C1", address="20 DONE ST",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Mechanical remodel of spec suite", land_use="",
         status="Closed - Complete", permit_date=date(2026, 6, 1)),
    # 3. Service work: like-for-like replacement, must never become a project.
    dict(permit_number="S1", natural_key="S1", address="30 SERVICE ST",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Remove & replace RTU package units like for like", land_use=""),
    # 4. Plumbing only: no mechanical evidence, so not discoverable as an HVAC opportunity.
    dict(permit_number="P1", natural_key="P1", address="40 PLUMB ST",
         permit_type="Commercial Plumbing Permit", permit_subtype="commercial_plumbing",
         work_description="New plumbing for new construction office building",
         land_use="OFFICE BUILDING", job_value=900_000.0),
    # 5. Suite pair sharing one building, to exercise grouping.
    dict(permit_number="SU1", natural_key="SU1", address="50 TOWER ST, 100",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Mechanical remodel of spec suite", land_use="",
         job_value=1_200_000.0, permit_date=date(2026, 9, 12)),
    dict(permit_number="SU2", natural_key="SU2", address="50 TOWER ST, 200",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Mechanical remodel of spec suite", land_use="",
         job_value=1_100_000.0, permit_date=date(2026, 9, 11)),
]


def build_database(path: Path) -> Database:
    """Create a database from the fixture permits, fully assembled and indexed."""
    db = Database(path)
    db.init_schema()
    db.init_app_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()

    for overrides in FIXTURE_PERMITS:
        permit = _permit(**overrides)
        db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()

    pipeline.assemble_and_classify()
    ensure_slugs(db)
    rebuild_index(db)
    return db


@pytest.fixture
def app_db(tmp_path):
    """A database plus a Flask app bound to it."""
    db_path = tmp_path / "app.db"
    db = build_database(db_path)
    db.close()

    cfg = AppConfig(database_path=db_path, secret_key="test-secret-key", debug=True)
    application = create_app(cfg)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app_db):
    return app_db.test_client()


@pytest.fixture
def session_client(app_db):
    """A client with a registered, signed-in user."""
    c = app_db.test_client()
    c.post(
        "/signup",
        data={
            "email": "estimator@example.com",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
            "display_name": "Estimator",
        },
        follow_redirects=True,
    )
    return c


@pytest.fixture
def fixture_db(tmp_path):
    """A raw handle on the fixture database, for assertions about stored rows."""
    db = build_database(tmp_path / "direct.db")
    yield db
    db.close()


#: The account level that gates the internal operations view.
ADMIN_LEVEL = "ADMIN"


def grant_admin(app_db, email: str) -> None:
    """Raise an account to the operator level.

    Mirrors what `flask grant-admin` does. The level is set directly in the database because
    there is deliberately no web route that can grant it.
    """
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        cursor = db.conn.execute(
            "UPDATE app_user SET access_level = ? WHERE email = ?",
            (ADMIN_LEVEL, email.lower()),
        )
        db.conn.commit()
        assert cursor.rowcount == 1, f"no account for {email!r}"
    finally:
        db.close()


@pytest.fixture
def admin_client(app_db):
    """A signed-in client whose account holds the operator level."""
    c = app_db.test_client()
    c.post(
        "/signup",
        data={
            "email": "operator@example.com",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
            "display_name": "Operator",
        },
        follow_redirects=True,
    )
    grant_admin(app_db, "operator@example.com")
    return c