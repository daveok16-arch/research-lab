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
import pathlib

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


# --- the start script chooses a writable data directory -------------------------

def _run_start(tmp_path, env_overrides, monkeypatch):
    """Run ops/start.sh with `python`/`initdb` stubbed out, and report how it chose its paths.

    The script is the thing that broke on a host without a disk, so the assertions below are about
    its own behaviour, not the app's. Stubbing the two init commands keeps the test to the path
    logic: they are the first thing the script runs, and they are not what is under test here.
    """
    import os
    import subprocess

    repo = pathlib.Path(__file__).resolve().parents[1]
    stub = tmp_path / "bin"
    stub.mkdir()
    # A `python` that records the env it was handed and exits successfully, so the script reaches
    # its path decisions without needing a real database.
    (stub / "python").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"DATA_DIR=$OPPINTEL_DATA_DIR\"\n"
        "echo \"DB=$OPPINTEL_DB\"\n"
        "echo \"BASE_URL=$BASE_URL\"\n"
        "echo \"ARGS=$*\"\n"
        "exit 0\n"
    )
    (stub / "python").chmod(0o755)

    env = {
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    env.update(env_overrides)
    for key in ("PYTHONPATH", "SECRET_KEY", "OPPINTEL_DATA_DIR", "OPPINTEL_DB", "RENDER", "PORT",
                "GROW_BACKFILL", "MAX_PAGES", "REFRESH_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    env.setdefault("RENDER_EXTERNAL_URL", "")
    return subprocess.run(
        ["bash", str(repo / "ops" / "start.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
        timeout=60,
    )


def test_start_script_does_not_assume_a_mounted_disk(tmp_path, monkeypatch):
    """A host with no disk must still start: `/var/data` does not exist on a Free instance.

    This is the failure that took a deploy down — the script defaulted the data directory to the
    mount path, so the first `mkdir` hit a read-only filesystem and the process exited.
    """
    result = _run_start(tmp_path, {"RENDER": "true", "PORT": "10000"}, monkeypatch)

    assert result.returncode == 0, result.stderr
    assert "DATA_DIR=" in result.stdout
    # It falls back to the checkout's own data/ rather than the mount path.
    assert "/var/data" not in result.stdout


def test_start_script_fails_loudly_when_the_data_directory_is_unwritable(tmp_path, monkeypatch):
    """An explicitly configured, unwritable path is a configuration error, not a traceback."""
    result = _run_start(
        tmp_path,
        {"RENDER": "true", "PORT": "10000", "OPPINTEL_DATA_DIR": "/proc/definitely-not-writable"},
        monkeypatch,
    )

    assert result.returncode == 1
    assert "cannot write to OPPINTEL_DATA_DIR" in result.stderr
    # The message names the cause, so the operator is not left reading a sqlite traceback.
    assert "disk is not attached" in result.stderr


def test_start_script_honours_a_writable_data_directory(tmp_path, monkeypatch):
    """The paid path: a mounted disk is used as given, and the database goes inside it."""
    disk = tmp_path / "mnt"
    result = _run_start(
        tmp_path,
        {"RENDER": "true", "PORT": "10000", "OPPINTEL_DATA_DIR": str(disk)},
        monkeypatch,
    )

    assert result.returncode == 0, result.stderr
    assert f"DATA_DIR={disk}" in result.stdout
    assert f"DB={disk}/oppintel.db" in result.stdout


# --- the public origin comes from the host --------------------------------------

def test_start_script_prefers_the_platform_url_over_loopback(tmp_path, monkeypatch):
    """A deployed host must not advertise http://127.0.0.1:<port> as its canonical origin.

    Render injects RENDER_EXTERNAL_URL. Ignoring it put a loopback address in every canonical tag
    and all 88 sitemap entries on a live service.
    """
    result = _run_start(
        tmp_path,
        {
            "RENDER": "true",
            "PORT": "10000",
            "RENDER_EXTERNAL_URL": "https://research-lab-9b9j.onrender.com",
        },
        monkeypatch,
    )

    assert result.returncode == 0, result.stderr
    assert "BASE_URL=https://research-lab-9b9j.onrender.com" in result.stdout
    assert "127.0.0.1" not in result.stdout


def test_start_script_explicit_base_url_wins(tmp_path, monkeypatch):
    """An operator-set BASE_URL (a custom domain) takes precedence over the platform default."""
    result = _run_start(
        tmp_path,
        {
            "RENDER": "true",
            "PORT": "10000",
            "BASE_URL": "https://hvac.example.com",
            "RENDER_EXTERNAL_URL": "https://research-lab-9b9j.onrender.com",
        },
        monkeypatch,
    )

    assert result.returncode == 0, result.stderr
    assert "BASE_URL=https://hvac.example.com" in result.stdout


def test_start_script_falls_back_to_loopback_only_off_platform(tmp_path, monkeypatch):
    """Loopback is the last resort, which is what makes `ops/start.sh` usable in a bare shell.

    Uses FOREGROUND=1 rather than the local branch: the local branch backgrounds the daemon, so
    the stub's output goes to automation.out and would not be observable here.
    """
    result = _run_start(tmp_path, {"FOREGROUND": "1", "PORT": "12000"}, monkeypatch)

    assert result.returncode == 0, result.stderr
    assert "BASE_URL=http://127.0.0.1:12000" in result.stdout


# --- the refresh deepens instead of re-reading the newest pages forever ---------

def _automate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "automate", pathlib.Path(__file__).resolve().parents[1] / "ops" / "automate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_depth_doubles_until_the_ceiling():
    """A fixed `--max-pages 3` never reached older history on the live instance."""
    automate = _automate()
    state: dict = {}
    depths = []
    for _ in range(10):
        depth = automate._next_depth(state, 3)
        depths.append(depth)
        state["backfill_pages"] = min(depth * automate.BACKFILL_GROWTH,
                                      automate.BACKFILL_CEILING_PAGES)

    assert depths[:4] == [3, 6, 12, 24]
    assert depths[-1] == automate.BACKFILL_CEILING_PAGES


def test_backfill_depth_never_shrinks_below_the_configured_floor():
    automate = _automate()
    assert automate._next_depth({"backfill_pages": 1}, 3) == 3
    assert automate._next_depth({}, 50) == 50


def test_start_script_enables_progressive_backfill_by_default(tmp_path, monkeypatch):
    result = _run_start(tmp_path, {"FOREGROUND": "1", "PORT": "12000"}, monkeypatch)

    assert result.returncode == 0, result.stderr
    assert "--grow-backfill" in result.stdout


def test_start_script_can_pin_the_depth(tmp_path, monkeypatch):
    """An operator can freeze the fetch width, e.g. to keep a small instance responsive."""
    result = _run_start(
        tmp_path, {"FOREGROUND": "1", "PORT": "12000", "GROW_BACKFILL": "0"}, monkeypatch
    )

    assert result.returncode == 0, result.stderr
    assert "--grow-backfill" not in result.stdout
