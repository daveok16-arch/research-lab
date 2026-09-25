"""Regression tests for the documented operations CLI.

The deployment runbook in the README tells an operator to run a fixed set of commands. A
command that raises on invocation is a release blocker: the runbook is wrong, and the failure
only appears on the host that owns the database rather than in the test suite.

This file exists because exactly that happened. The `monitor` command imported
`build_alerts` from `..alerts` (the intelligence package) instead of `.alerts` (the
application package), so it raised `ModuleNotFoundError` every time it ran. No test invoked
the command, so the 595-test suite passed while the documented deployment step was broken.

These tests invoke each documented command through Flask's CLI runner against a real
application and a real database, which is the only level at which an import-path error of
this kind is visible.
"""

from __future__ import annotations

import pytest

from conftest_app import build_database
from oppintel.app.config import AppConfig
from oppintel.app.main import create_app


@pytest.fixture
def cli_app(tmp_path):
    """A real application bound to a real database, for exercising the CLI."""
    db_path = tmp_path / "cli.db"
    db = build_database(db_path)
    db.close()
    cfg = AppConfig(database_path=db_path, secret_key="test-secret-key", debug=True)
    application = create_app(cfg)
    application.config["TESTING"] = True
    return application, db_path


def _run(app, *args):
    """Invoke a CLI command and assert it completed without raising.

    An exception is surfaced explicitly, because Flask's runner catches it and reports a
    non-zero exit code; asserting on the exit code alone would leave the traceback unread.
    """
    result = app.test_cli_runner().invoke(args=list(args))
    assert result.exit_code == 0, (
        f"`{' '.join(args)}` failed with exit code {result.exit_code}:\n"
        f"{result.output}\n{result.exception!r}"
    )
    assert result.exception is None, f"`{' '.join(args)}` raised: {result.exception!r}"
    return result


# --- the specific regression ----------------------------------------------------

def test_monitor_command_runs(cli_app):
    """The documented monitoring pass must execute.

    This is the regression test for the `.alerts` import path. Before the fix it raised
    `ModuleNotFoundError: No module named 'oppintel.alerts'`; the assertion on the exception
    is what makes the failure readable if the path regresses again.
    """
    app, _ = cli_app
    result = _run(app, "monitor")
    assert "alerts created:" in result.output


def test_monitor_command_can_skip_alerts(cli_app):
    """The documented `--no-alerts` flag must work as well as the default path."""
    app, _ = cli_app
    result = _run(app, "monitor", "--no-alerts")
    assert "monitoring skipped" in result.output


def test_monitor_command_is_idempotent(cli_app):
    """A second run with no new data must create no alerts, as the runbook states."""
    app, _ = cli_app
    _run(app, "monitor")
    second = _run(app, "monitor")
    assert "alerts created: 0" in second.output


def test_monitor_imports_build_alerts_from_the_application_package():
    """Assert the module location directly, so the import cannot silently move again.

    The application package is `oppintel.app.alerts`. An `oppintel.alerts` module does not
    exist and must not be introduced, because the intelligence layer must not depend on the
    application layer.
    """
    from oppintel.app.alerts import build_alerts

    assert callable(build_alerts)
    assert build_alerts.__module__.startswith("oppintel.app."), (
        "build_alerts must live in the application package, not the intelligence package"
    )

    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("oppintel.alerts")


# --- every other documented command ---------------------------------------------

def test_init_app_command_runs(cli_app):
    app, _ = cli_app
    result = _run(app, "init-app")
    assert "Application schema ready" in result.output


def test_build_search_index_command_runs(cli_app):
    app, _ = cli_app
    result = _run(app, "build-search-index")
    assert "slugs created:" in result.output
    assert "projects indexed:" in result.output


def test_report_quality_command_runs(cli_app):
    app, _ = cli_app
    _run(app, "report-quality")


def test_grant_and_revoke_admin_commands_run(cli_app):
    """The operator commands must work against a real account."""
    from werkzeug.security import generate_password_hash

    from oppintel.db import Database

    app, db_path = cli_app
    db = Database(db_path)
    db.conn.execute(
        """
        INSERT INTO app_user (email, password_hash, display_name, access_level, is_active,
            created_at)
        VALUES (?, ?, 'Ops', 'FREE', 1, '2026-09-01T00:00:00+00:00')
        """,
        ("ops@example.com", generate_password_hash("correct-horse-battery")),
    )
    db.conn.commit()
    db.close()

    assert "Granted ADMIN" in _run(app, "grant-admin", "ops@example.com").output

    db = Database(db_path)
    try:
        level = db.conn.execute(
            "SELECT access_level FROM app_user WHERE email = 'ops@example.com'"
        ).fetchone()["access_level"]
        assert level == "ADMIN"
    finally:
        db.close()

    assert "Revoked ADMIN" in _run(app, "revoke-admin", "ops@example.com").output


def test_set_plan_command_runs(cli_app):
    """The documented plan command must work, and must be the only way to grant a plan."""
    from werkzeug.security import generate_password_hash

    from oppintel.db import Database

    app, db_path = cli_app
    db = Database(db_path)
    db.conn.execute(
        """
        INSERT INTO app_user (email, password_hash, display_name, access_level, is_active,
            created_at)
        VALUES (?, ?, 'Buyer', 'FREE', 1, '2026-09-01T00:00:00+00:00')
        """,
        ("buyer@example.com", generate_password_hash("correct-horse-battery")),
    )
    db.conn.commit()
    db.close()

    assert "Set buyer@example.com to plan PRO" in _run(
        app, "set-plan", "buyer@example.com", "PRO"
    ).output

    db = Database(db_path)
    try:
        row = db.conn.execute(
            "SELECT plan_id, status FROM subscription WHERE user_id = "
            "(SELECT id FROM app_user WHERE email = 'buyer@example.com')"
        ).fetchone()
        assert row["plan_id"] == "PRO"
        assert row["status"] == "ACTIVE"
    finally:
        db.close()


def test_every_documented_command_is_registered(cli_app):
    """The runbook's command list must match what the application actually registers."""
    app, _ = cli_app
    documented = {
        "init-app",
        "build-search-index",
        "monitor",
        "report-quality",
        "grant-admin",
        "revoke-admin",
        "set-plan",
    }
    assert documented <= set(app.cli.commands), (
        f"documented but not registered: {sorted(documented - set(app.cli.commands))}"
    )
