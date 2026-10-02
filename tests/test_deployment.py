"""Health-check and deployment-configuration tests.

Two things are asserted here:

* `/healthz` is a usable probe. A platform restarts the service on a failed probe, so the route
  must return 503 when the database is unusable and 200 when the service is merely empty (an
  empty first deploy must not produce a restart loop).
* The database path is configurable through the environment, because a host mounts a disk and
  points the pipeline at it rather than writing into the checkout.
"""

from __future__ import annotations

import importlib
import json

from oppintel.db import Database


# --- the health route -----------------------------------------------------------

def test_health_reports_ok_with_counts(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["status"] == "ok"
    assert payload["projects"] > 0
    assert payload["permits"] > 0


def test_health_reports_the_database_path(client, app_db):
    payload = json.loads(client.get("/healthz").get_data(as_text=True))
    expected = str(app_db.config["APP_CONFIG"].database_path)
    assert payload["database"] == expected


def test_health_is_json_not_html(client):
    response = client.get("/healthz")
    assert response.mimetype == "application/json"


def test_health_discloses_no_project_content(client):
    """Counts are fine; a name, address or permit number would not be."""
    body = client.get("/healthz").get_data(as_text=True)
    payload = json.loads(body)
    assert set(payload) == {"status", "database", "projects", "permits"}
    for leak in ("ROSS", "TOWER", "MAIN", "permit_number", "address"):
        assert leak not in body


def test_health_stays_up_when_the_database_is_empty(tmp_path):
    """An empty database is a first deploy, not a fault: 200, or the platform restarts forever."""
    from oppintel.app.config import AppConfig
    from oppintel.app.main import create_app

    db_path = tmp_path / "empty.db"
    db = Database(db_path)
    db.init_schema()
    db.init_app_schema()
    db.close()

    app = create_app(AppConfig(database_path=db_path, secret_key="test", debug=True))
    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True))["status"] == "empty"


def test_health_returns_503_when_the_database_is_unreachable(app_db):
    """Called directly, because the request hook opens the database before the view runs.

    A handle whose query raises stands in for the mounted disk being absent or unreadable,
    which is the condition a restart can actually recover from.
    """
    from flask import g

    with app_db.test_request_context("/healthz"):
        g.db = _BrokenDb()
        body, status = app_db.view_functions["healthz"]()

    assert status == 503
    assert json.loads(body.get_data(as_text=True))["status"] == "database_unavailable"


class _BrokenDb:
    """A database handle whose connection fails on query, like an unreadable disk."""

    class _Conn:
        def execute(self, *_args, **_kwargs):
            raise OSError("database disk image is malformed")

    conn = _Conn()

    def close(self) -> None:
        pass


# --- the route is operational, not content --------------------------------------

def test_health_is_excluded_from_the_sitemap(client):
    assert "/healthz" not in client.get("/sitemap.xml").get_data(as_text=True)


def test_health_is_disallowed_by_robots(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /healthz" in body


# --- the database path comes from the environment -------------------------------

def test_cli_database_path_honours_the_environment(monkeypatch, tmp_path):
    """A host points the pipeline at a mounted disk without repeating `--db`."""
    import oppintel.cli as cli

    target = tmp_path / "mounted" / "oppintel.db"
    monkeypatch.setenv("OPPINTEL_DB", str(target))
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.DEFAULT_DB == target
    finally:
        monkeypatch.delenv("OPPINTEL_DB", raising=False)
        importlib.reload(cli)


def test_cli_database_path_defaults_to_the_repo(monkeypatch):
    import oppintel.cli as cli
    from oppintel.config import DATA_DIR

    monkeypatch.delenv("OPPINTEL_DB", raising=False)
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.DEFAULT_DB == DATA_DIR / "oppintel.db"
    finally:
        importlib.reload(cli)
