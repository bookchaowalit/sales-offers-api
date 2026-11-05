"""Characterizes cellkit.db: names and paths, the hosted URL rules, migration guards, and Neon provisioning (faked over HTTP)."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cellkit_support as support
import pytest

from cellkit import db
from cellkit.db import HostedCluster, LocalCluster, Names, default_cluster

SECRET_API = "api-SECRET-do-not-print-123456"


# --- names and paths --------------------------------------------------------------------------------


def test_names_follow_the_cell_name(mini_cell):
    names = Names(mini_cell)
    assert names.database == "kit_sample"
    assert names.migrator == "kit_sample_migrator"
    assert names.runtime_roles == ("kit_sample_api", "kit_sample_publisher")
    assert names.migrations == mini_cell.root / "migrations"
    assert names.roles_sql == mini_cell.root / "db" / "roles.sql"


def test_short_role_names_drop_the_schema_prefix(mini_cell):
    names = Names(mini_cell)
    assert names.short("kit_sample_migrator") == "migrator"
    assert names.short("kit_sample_api") == "api"
    assert names.short("kit_sample_publisher") == "publisher"


def test_the_secrets_file_is_in_the_home_config_unless_overridden(
    mini_cell, monkeypatch, tmp_path
):
    monkeypatch.delenv("KIT_SAMPLE_DB_SECRETS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        Names(mini_cell).secrets_file
        == tmp_path / ".config" / "solo-kit-sample" / "db.env"
    )
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(tmp_path / "custom.env"))
    assert Names(mini_cell).secrets_file == tmp_path / "custom.env"


def test_the_bundled_postgres_binaries_are_preferred_over_the_path(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("KIT_SAMPLE_PG_BIN", raising=False)
    bundled = db.pg_bin_dir("KIT_SAMPLE")
    assert (bundled / "pg_ctl").exists()
    monkeypatch.setenv("KIT_SAMPLE_PG_BIN", str(tmp_path))
    assert db.pg_bin_dir("KIT_SAMPLE") == tmp_path


def test_without_binaries_the_error_says_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("KIT_SAMPLE_PG_BIN", raising=False)
    monkeypatch.setitem(sys.modules, "pgserver", None)  # makes the import fail
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="KIT_SAMPLE_PG_BIN"):
        db.pg_bin_dir("KIT_SAMPLE")


def test_a_pg_ctl_on_the_path_is_the_last_resort(monkeypatch):
    monkeypatch.delenv("KIT_SAMPLE_PG_BIN", raising=False)
    monkeypatch.setitem(sys.modules, "pgserver", None)
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/pg/bin/pg_ctl")
    assert db.pg_bin_dir("KIT_SAMPLE") == Path("/opt/pg/bin")


def test_stopping_a_cluster_that_is_not_running_does_nothing(
    mini_cell, tmp_path, monkeypatch
):
    calls: list[Any] = []
    monkeypatch.setattr(LocalCluster, "_run", lambda self, *args: calls.append(args))
    LocalCluster(Names(mini_cell), tmp_path / "pgdata", tmp_path / "sock").stop()
    assert calls == []


def test_the_default_cluster_is_local_unless_hosted_is_asked_for(
    mini_cell, monkeypatch, tmp_path
):
    monkeypatch.delenv("KIT_SAMPLE_DB_MODE", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    local = default_cluster(mini_cell)
    assert isinstance(local, LocalCluster)
    assert local.data_dir == mini_cell.root / ".runtime" / "pgdata"
    assert local.socket_dir == tmp_path / "solo-kit-sample"

    monkeypatch.setenv("KIT_SAMPLE_DB_MODE", "hosted")
    assert isinstance(default_cluster(mini_cell), HostedCluster)


# --- hosted URLs ----------------------------------------------------------------------------------


def cluster_for(cluster, cell, root: Path):
    """The same throwaway server, read as if the cell's migrations lived in ``root``."""
    return dataclasses.replace(
        cluster, names=Names(dataclasses.replace(cell, root=root))
    )


def write_secrets(path: Path, lines: list[str], mode: int = 0o600) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_hosted_urls_come_from_the_owner_only_file_and_the_environment(
    mini_cell, monkeypatch, tmp_path
):
    path = write_secrets(
        tmp_path / "db.env",
        [
            "# owner-only",
            f"KIT_SAMPLE_DB_URL_API=postgresql://kit_sample_api:{SECRET_API}@h/db",
            "KIT_SAMPLE_DB_URL_MIGRATOR=postgresql://m@h/db",
            "OTHER_KEY=ignored",
        ],
    )
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(path))
    monkeypatch.setenv("KIT_SAMPLE_DB_URL_PUBLISHER", "postgresql://p@h/db")
    monkeypatch.setenv(
        "KIT_SAMPLE_DB_URL_MIGRATOR", ""
    )  # an empty variable does not override
    cluster = HostedCluster.from_env(Names(mini_cell))
    assert cluster.urls == {
        "api": f"postgresql://kit_sample_api:{SECRET_API}@h/db",
        "migrator": "postgresql://m@h/db",
        "publisher": "postgresql://p@h/db",
    }


def test_a_variable_overrides_the_file(mini_cell, monkeypatch, tmp_path):
    path = write_secrets(
        tmp_path / "db.env", ["KIT_SAMPLE_DB_URL_API=postgresql://from-file"]
    )
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(path))
    monkeypatch.setenv("KIT_SAMPLE_DB_URL_API", "postgresql://from-env")
    assert (
        HostedCluster.from_env(Names(mini_cell)).urls["api"] == "postgresql://from-env"
    )


def test_a_secrets_file_readable_by_others_is_refused(mini_cell, monkeypatch, tmp_path):
    path = write_secrets(
        tmp_path / "db.env", [f"KIT_SAMPLE_DB_URL_API={SECRET_API}"], mode=0o644
    )
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(path))
    with pytest.raises(SystemExit, match="readable by its owner only") as info:
        HostedCluster.from_env(Names(mini_cell))
    assert SECRET_API not in str(info.value)


def test_a_missing_url_names_the_variable_not_a_value(mini_cell, monkeypatch, tmp_path):
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(tmp_path / "none.env"))
    cluster = HostedCluster(
        Names(mini_cell), {"api": f"postgresql://x:{SECRET_API}@h/db"}
    )
    with pytest.raises(SystemExit, match="KIT_SAMPLE_DB_URL_MIGRATOR") as info:
        cluster.connect("kit_sample_migrator")
    assert SECRET_API not in str(info.value)


def test_a_hosted_connection_uses_a_short_connect_timeout(mini_cell, monkeypatch):
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        db.psycopg,
        "connect",
        lambda url, **kwargs: seen.append((url, kwargs)) or "conn",
    )
    cluster = HostedCluster(Names(mini_cell), {"api": "postgresql://api@h/db"})
    assert cluster.connect("kit_sample_api") == "conn"
    assert seen == [("postgresql://api@h/db", {"connect_timeout": 15})]


# --- migrations ---------------------------------------------------------------------------------


def test_migrating_again_applies_nothing(mini_cluster):
    assert db.migrate(mini_cluster) == []


def test_an_applied_migration_that_was_edited_is_refused(
    mini_cell, mini_cluster, tmp_path
):
    root = tmp_path / "edited"
    (root / "migrations").mkdir(parents=True)
    original = (mini_cell.root / "migrations" / "0001_kit_sample.sql").read_text(
        encoding="utf-8"
    )
    (root / "migrations" / "0001_kit_sample.sql").write_text(
        original + "\n-- edited\n", encoding="utf-8"
    )
    edited = cluster_for(mini_cluster, mini_cell, root)
    with pytest.raises(
        SystemExit, match=r"applied migration was edited: 0001_kit_sample\.sql"
    ):
        db.migrate(edited)


def test_a_cell_without_migrations_cannot_be_migrated(
    mini_cell, mini_cluster, tmp_path
):
    empty = cluster_for(mini_cluster, mini_cell, tmp_path)
    with pytest.raises(SystemExit, match="no migrations found"):
        db.migrate(empty)


def test_a_new_migration_is_applied_once_and_recorded(
    mini_cell, mini_cluster, tmp_path
):
    root = tmp_path / "next"
    (root / "migrations").mkdir(parents=True)
    original = (mini_cell.root / "migrations" / "0001_kit_sample.sql").read_text(
        encoding="utf-8"
    )
    (root / "migrations" / "0001_kit_sample.sql").write_text(original, encoding="utf-8")
    (root / "migrations" / "0002_probe.sql").write_text(
        "CREATE TABLE kit_sample.migration_probe (id integer);\n", encoding="utf-8"
    )
    next_cluster = cluster_for(mini_cluster, mini_cell, root)
    assert db.migrate(next_cluster) == ["0002_probe.sql"]
    assert db.migrate(next_cluster) == []


def test_the_no_redirect_handler_never_follows_a_redirect():
    handler = db._NoRedirect()
    request = urllib.request.Request("https://console.neon.tech/api/v2/projects")
    assert (
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://elsewhere.example"
        )
        is None
    )


# --- Neon provisioning (HTTP faked) -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def patch_neon_http(monkeypatch, responses: list[Any]) -> list[urllib.request.Request]:
    """Each call pops one scripted answer: bytes to return, or an HTTPError to raise."""
    seen: list[urllib.request.Request] = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            answer = responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return FakeResponse(answer)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return seen


def test_the_neon_client_sends_the_key_as_a_bearer_and_parses_json(monkeypatch):
    seen = patch_neon_http(monkeypatch, [b'{"projects": []}'])
    assert db._neon("neon-key-123", "GET", "/projects?search=x") == {"projects": []}
    (request,) = seen
    assert request.get_method() == "GET"
    assert request.full_url == f"{db.NEON_API}/projects?search=x"
    assert request.get_header("Authorization") == "Bearer neon-key-123"


def test_an_empty_neon_answer_is_an_empty_object_and_bodies_are_sent_as_json(
    monkeypatch,
):
    seen = patch_neon_http(monkeypatch, [b"", b"{}"])
    assert db._neon("k", "GET", "/projects/x") == {}
    assert db._neon("k", "POST", "/projects", {"project": {"name": "p"}}) == {}
    assert json.loads(seen[1].data) == {"project": {"name": "p"}}


def test_a_locked_project_is_retried_before_giving_up(monkeypatch):
    locked = urllib.error.HTTPError("u", 423, "locked", hdrs=None, fp=None)
    seen = patch_neon_http(monkeypatch, [locked, locked, b'{"ok": true}'])
    assert db._neon("k", "POST", "/projects/x/roles", {}) == {"ok": True}
    assert len(seen) == 3


def test_a_neon_error_stops_with_its_status_and_no_key(monkeypatch):
    err = urllib.error.HTTPError("u", 404, "nf", hdrs=None, fp=None)
    patch_neon_http(monkeypatch, [err])
    with pytest.raises(SystemExit, match=r"neon GET /projects/x: 404") as info:
        db._neon("neon-key-123", "GET", "/projects/x?search=secret")
    assert "neon-key-123" not in str(info.value)
    assert "search" not in str(info.value)


def test_urls_quote_the_role_and_the_password():
    url = db._url("ep.example", "kit_sample", "kit_sample_api", "p@ss/w:rd")
    assert url == (
        "postgresql://kit_sample_api:p%40ss%2Fw%3Ard@ep.example/kit_sample"
        "?sslmode=require&channel_binding=require"
    )


def test_a_failed_private_write_leaves_no_temporary_file_and_no_partial_secret(
    tmp_path, monkeypatch
):
    target = tmp_path / "secrets" / "db.env"

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        db._write_private(target, "KIT_SAMPLE_DB_URL_API=postgresql://x\n")
    assert not target.exists()
    assert list(target.parent.iterdir()) == [], "the temporary file is removed"


def test_private_files_are_written_owner_only_and_atomically(tmp_path):
    target = tmp_path / "secrets" / "db.env"
    db._write_private(target, "first\n")
    db._write_private(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in target.parent.iterdir()) == ["db.env"], (
        "no temporary file is left behind"
    )


class FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeConn:
    def __init__(self, existing_roles: set[str]) -> None:
        self.existing = existing_roles
        self.statements: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> FakeResult:
        self.statements.append((query, params))
        if params and params[0] in self.existing:
            return FakeResult((1,))
        return FakeResult(None)


def neon_fake(project_exists: bool, migrator_role_exists: bool = False):
    calls: list[tuple[str, str, Any]] = []

    def neon(key: str, method: str, path: str, body: Any = None) -> dict[str, Any]:
        calls.append((method, path, body))
        if method == "GET" and path.startswith("/projects?search="):
            found = (
                [{"name": "kit-sample-repo", "id": "proj-1"}] if project_exists else []
            )
            return {"projects": found}
        if method == "POST" and path == "/projects":
            return {"project": {"id": "proj-new"}}
        if path.endswith("/branches"):
            return {"branches": [{"id": "br-1", "default": True}]}
        if path.endswith("/endpoints"):
            return {
                "endpoints": [
                    {"branch_id": "br-1", "type": "read_write", "host": "ep.example"}
                ]
            }
        if method == "GET" and path.endswith("/roles"):
            return {
                "roles": [{"name": "kit_sample_migrator"}]
                if migrator_role_exists
                else []
            }
        if path.endswith("/reveal_password"):
            return {"password": "migrator-pass"}
        if method == "GET" and path.endswith("/databases"):
            return {"databases": []}
        return {}

    return neon, calls


def provision_with(
    monkeypatch,
    mini_cell,
    tmp_path,
    *,
    project_exists=True,
    existing_roles=frozenset(),
    migrator_role_exists=False,
):
    neon, calls = neon_fake(project_exists, migrator_role_exists)
    connections: list[FakeConn] = []
    urls: list[str] = []

    def connect(url, **kwargs):
        urls.append(url)
        conn = FakeConn(set(existing_roles))
        connections.append(conn)
        return conn

    migrated: list[Any] = []
    monkeypatch.setenv("NEON_API_KEY", "fake-neon-key")
    monkeypatch.setenv("KIT_SAMPLE_DB_SECRETS", str(tmp_path / "secrets" / "db.env"))
    monkeypatch.setattr(db, "_neon", neon)
    monkeypatch.setattr(db.psycopg, "connect", connect)
    monkeypatch.setattr(db, "migrate", lambda cluster: migrated.append(cluster) or [])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    project = db.provision(mini_cell, "kit-sample-repo", "aws-ap-southeast-1")
    return project, calls, connections, urls, migrated


def test_provisioning_writes_owner_only_urls_and_migrates(
    mini_cell, monkeypatch, tmp_path, capsys
):
    project, calls, connections, urls, migrated = provision_with(
        monkeypatch, mini_cell, tmp_path, project_exists=True
    )
    assert project == "proj-1"
    assert not any(
        method == "POST" and path == "/projects" for method, path, _ in calls
    ), "an existing project is reused"

    secrets = tmp_path / "secrets" / "db.env"
    text = secrets.read_text(encoding="utf-8")
    assert secrets.stat().st_mode & 0o777 == 0o600
    for name in ("MIGRATOR", "API", "PUBLISHER"):
        assert f"KIT_SAMPLE_DB_URL_{name}=postgresql://" in text
    assert "migrator-pass" in text
    assert "ep.example/kit_sample" in text

    assert urls[0].startswith(
        "postgresql://kit_sample_migrator:migrator-pass@ep.example/kit_sample"
    )
    assert [params for _query, params in connections[0].statements if params] == [
        ("kit_sample_api",),
        ("kit_sample_publisher",),
    ]
    assert len(migrated) == 1
    assert sorted(migrated[0].urls) == ["api", "migrator", "publisher"]
    out = capsys.readouterr().out
    assert "migrator-pass" not in out
    assert "neon-key-SECRET" not in out


def test_provisioning_creates_a_missing_project_and_reuses_known_passwords(
    mini_cell, monkeypatch, tmp_path
):
    secrets = tmp_path / "secrets" / "db.env"
    secrets.parent.mkdir(parents=True)
    write_secrets(
        secrets,
        [
            "KIT_SAMPLE_DB_URL_API=postgresql://kit_sample_api:kept-api-pass@old/kit_sample"
        ],
    )

    project, calls, connections, _urls, _ = provision_with(
        monkeypatch,
        mini_cell,
        tmp_path,
        project_exists=False,
        existing_roles=frozenset({"kit_sample_api"}),
    )
    assert project == "proj-new"
    created = [
        body for method, path, body in calls if method == "POST" and path == "/projects"
    ]
    assert created == [
        {
            "project": {
                "name": "kit-sample-repo",
                "region_id": "aws-ap-southeast-1",
                "pg_version": 16,
            }
        }
    ]
    assert "@ep.example/kit_sample" in secrets.read_text(encoding="utf-8")
    assert "kept-api-pass" in secrets.read_text(encoding="utf-8"), (
        "a known password is kept, not rotated"
    )
    # the existing api role is altered, the publisher role is created
    statements = [
        query for query, params in connections[0].statements if params is None
    ]
    assert len(statements) == 5  # two role statements, then the three database grants


def test_provisioning_needs_the_neon_key(mini_cell, monkeypatch, tmp_path):
    monkeypatch.delenv("NEON_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="NEON_API_KEY is not set"):
        db.provision(mini_cell, "kit-sample-repo", "aws-ap-southeast-1")


mini_root = support.mini_root
mini_cell = support.mini_cell
mini_cluster = support.mini_cluster
