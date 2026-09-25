"""Shared test fixtures.

Tests exercise real code paths: real normalization, real assembly, real classification, and
a real SQLite database. No mocks are used. The only thing that is not real is the source
payloads, which are copied from live responses captured during Phase 1 reconnaissance and
are stored under tests/fixtures/.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from oppintel.config import active_trade  # noqa: E402
from oppintel.models import Permit  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def trade():
    return active_trade()


@pytest.fixture
def observed_at():
    return datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_permit(**overrides) -> Permit:
    """Build a Permit with sensible defaults; override any field per test."""
    defaults = dict(
        source_id="fort_worth_permits",
        permit_number="PB26-00001",
        natural_key="PB26-00001::Commercial Building Permit",
        permit_type="Commercial Building Permit",
        permit_subtype="New",
        permit_date=date(2026, 9, 1),
        status="Issued",
        address="100 MAIN ST",
        city="Fort Worth",
        state="TX",
        zip_code="76102",
        work_description="New construction of medical office building",
        land_use="Office",
        specific_use=None,
        job_value=5_000_000.0,
        square_footage=40_000.0,
        owner="ACME HEALTH LLC",
        contractor="BUILDER CO",
        is_commercial=True,
        source_url="https://example.gov/permits/PB26-00001",
        source_date=date(2026, 9, 1),
    )
    defaults.update(overrides)
    return Permit(**defaults)

# Application-layer fixtures live in conftest_app.py. Registering it as a plugin makes the
# app fixtures available to every test module without per-module declarations.
pytest_plugins = ["conftest_app"]
