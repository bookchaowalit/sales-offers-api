"""Characterizes cellkit.server: the entry points' configuration checks, the wiring they hand to uvicorn, and the publish/db commands."""

from __future__ import annotations

import contextlib
import json
import shutil
from typing import Any

import pytest
from cellkit_support import FIXTURE_SPEC

from cellkit import auth, server

FIXTURE_DIR = FIXTURE_SPEC.parent
PLATFORM = "https://platform.example"
PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nTEST\n-----END PUBLIC KEY-----"
PUBLISHER_TOKEN = "publisher-SECRET-do-not-print-123456"


@pytest.fixture(autouse=True)
def app_root(monkeypatch):
    """Every entry point finds the fixture cell; no test depends on this repository's cell.yaml."""
    monkeypatch.setenv("CELL_APP_ROOT", str(FIXTURE_DIR))
    for name in (
        "KIT_SAMPLE_DB_MODE",
        "KIT_SAMPLE_JWT_PUBLIC_KEY_PEM",
        "KIT_SAMPLE_JWT_JWKS_URL",
        "KIT_SAMPLE_JWT_ISSUER",
        "KIT_SAMPLE_PUBLISHER_TOKEN",
        "EMPIRE_PLATFORM_URL",
        "HOST",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def configure_api(monkeypatch, **env: str) -> dict[str, Any]:
    """Patch the framework pieces api() imports; returns what they were called with."""
    captured: dict[str, Any] = {}

    def fake_create_app(
        cell, connect, verifier, platform, publisher_token=lambda: None
    ):
        captured.update(
            cell=cell,
            verifier=verifier,
            platform=platform,
            publisher_token=publisher_token,
        )
        return "app-sentinel"

    class FakeVerifier:
        def __init__(self, pem, issuer, audience, jwks=None):
            captured["verifier_args"] = (pem, issuer, audience, jwks)

    class FakeJwks:
        def __init__(self, url):
            captured["jwks_url"] = url

    def fake_run(app, **kwargs):
        captured["uvicorn"] = (app, kwargs)

    import uvicorn

    monkeypatch.setattr("cellkit.app.create_app", fake_create_app)
    monkeypatch.setattr(auth, "TokenVerifier", FakeVerifier)
    monkeypatch.setattr(auth, "JwksKeys", FakeJwks)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return captured


# --- the cell in the application root ---------------------------------------------------------------


def test_the_cell_comes_from_the_app_root_then_the_working_directory(monkeypatch):
    assert server.cell().name == "kit-sample"
    monkeypatch.delenv("CELL_APP_ROOT")
    monkeypatch.chdir(FIXTURE_DIR)
    assert server.cell().name == "kit-sample"


# --- <cell>-api -----------------------------------------------------------------------------------


def test_the_api_refuses_to_start_without_a_key_an_issuer_or_the_platform(monkeypatch):
    configure_api(monkeypatch)
    with pytest.raises(
        SystemExit, match="KIT_SAMPLE_JWT_PUBLIC_KEY_PEM or KIT_SAMPLE_JWT_JWKS_URL"
    ):
        server.api()


def test_the_api_starts_on_loopback_with_the_spec_port_by_default(monkeypatch):
    captured = configure_api(
        monkeypatch,
        KIT_SAMPLE_JWT_PUBLIC_KEY_PEM=PUBLIC_KEY,
        KIT_SAMPLE_JWT_ISSUER="empire-platform",
        EMPIRE_PLATFORM_URL=PLATFORM,
    )
    server.api()
    app, kwargs = captured["uvicorn"]
    assert app == "app-sentinel"
    assert kwargs == {"host": "127.0.0.1", "port": 3900, "proxy_headers": False}
    assert captured["verifier_args"] == (
        PUBLIC_KEY,
        "empire-platform",
        "kit-sample-api",
        None,
    )
    assert captured["platform"].base == PLATFORM


def test_hosted_mode_listens_on_all_interfaces_and_honours_host_and_port(monkeypatch):
    captured = configure_api(
        monkeypatch,
        KIT_SAMPLE_DB_MODE="hosted",
        KIT_SAMPLE_JWT_PUBLIC_KEY_PEM=PUBLIC_KEY,
        KIT_SAMPLE_JWT_ISSUER="empire-platform",
        EMPIRE_PLATFORM_URL=PLATFORM,
        HOST="10.1.2.3",
        PORT="4100",
    )
    server.api()
    assert captured["uvicorn"][1]["port"] == 4100
    assert captured["uvicorn"][1]["host"] == "10.1.2.3"

    monkeypatch.delenv("HOST")
    monkeypatch.delenv("PORT")
    server.api()
    assert captured["uvicorn"][1]["host"] == "0.0.0.0"
    assert captured["uvicorn"][1]["port"] == 3900


def test_hosted_mode_refuses_the_development_issuer(monkeypatch):
    configure_api(
        monkeypatch,
        KIT_SAMPLE_DB_MODE="hosted",
        KIT_SAMPLE_JWT_PUBLIC_KEY_PEM=PUBLIC_KEY,
        KIT_SAMPLE_JWT_ISSUER="empire-platform-dev",
        EMPIRE_PLATFORM_URL=PLATFORM,
    )
    with pytest.raises(SystemExit, match="hosted mode refuses the dev issuer"):
        server.api()


def test_the_jwks_url_must_be_https_and_is_passed_to_the_verifier(monkeypatch):
    captured = configure_api(
        monkeypatch,
        KIT_SAMPLE_JWT_JWKS_URL=f"{PLATFORM}/.well-known/jwks.json",
        KIT_SAMPLE_JWT_ISSUER="empire-platform",
        EMPIRE_PLATFORM_URL=PLATFORM,
    )
    server.api()
    assert captured["jwks_url"] == f"{PLATFORM}/.well-known/jwks.json"
    assert captured["verifier_args"][3] is not None

    monkeypatch.setenv("KIT_SAMPLE_JWT_JWKS_URL", "http://keys.example/jwks.json")
    with pytest.raises(ValueError, match="KIT_SAMPLE_JWT_JWKS_URL must be https"):
        server.api()


def test_the_publisher_token_is_read_from_the_environment_when_needed(monkeypatch):
    captured = configure_api(
        monkeypatch,
        KIT_SAMPLE_JWT_PUBLIC_KEY_PEM=PUBLIC_KEY,
        KIT_SAMPLE_JWT_ISSUER="empire-platform",
        EMPIRE_PLATFORM_URL=PLATFORM,
    )
    server.api()
    assert captured["publisher_token"]() is None
    monkeypatch.setenv("KIT_SAMPLE_PUBLISHER_TOKEN", PUBLISHER_TOKEN)
    assert captured["publisher_token"]() == PUBLISHER_TOKEN


# --- <cell>-publish -------------------------------------------------------------------------------


def test_publish_needs_its_token_and_the_platform_and_says_so_on_stderr(
    monkeypatch, capsys
):
    assert server.publish([]) == 2
    assert (
        "set KIT_SAMPLE_PUBLISHER_TOKEN and EMPIRE_PLATFORM_URL"
        in capsys.readouterr().err
    )


class FakeCluster:
    def __init__(self) -> None:
        self.roles: list[str] = []

    def connect(self, role: str):
        self.roles.append(role)
        return contextlib.nullcontext("connection")


def patch_publish(monkeypatch, counts: dict[str, int]):
    calls: dict[str, Any] = {"cluster": FakeCluster()}

    def fake_publish_pending(conn, schema, platform, token, limit=100):
        calls.update(
            conn=conn, schema=schema, platform=platform, token=token, limit=limit
        )
        return counts

    import cellkit.db
    import cellkit.publisher

    monkeypatch.setattr(cellkit.db, "default_cluster", lambda spec: calls["cluster"])
    monkeypatch.setattr(cellkit.publisher, "publish_pending", fake_publish_pending)
    return calls


def test_publish_sweeps_as_the_publisher_role_and_prints_counts(monkeypatch, capsys):
    monkeypatch.setenv("KIT_SAMPLE_PUBLISHER_TOKEN", PUBLISHER_TOKEN)
    monkeypatch.setenv("EMPIRE_PLATFORM_URL", PLATFORM)
    calls = patch_publish(monkeypatch, {"published": 4, "failed": 0})

    assert server.publish(["--batch", "7"]) == 0

    assert calls["cluster"].roles == ["kit_sample_publisher"]
    assert calls["conn"] == "connection"
    assert calls["schema"] == "kit_sample"
    assert calls["token"] == PUBLISHER_TOKEN
    assert calls["limit"] == 7
    assert calls["platform"].base == PLATFORM
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "event": "kit-sample.publish",
        "published": 4,
        "failed": 0,
    }
    assert PUBLISHER_TOKEN not in out


def test_a_sweep_with_failures_exits_nonzero(monkeypatch):
    monkeypatch.setenv("KIT_SAMPLE_PUBLISHER_TOKEN", PUBLISHER_TOKEN)
    monkeypatch.setenv("EMPIRE_PLATFORM_URL", PLATFORM)
    patch_publish(monkeypatch, {"published": 1, "failed": 2})
    assert server.publish([]) == 1


def test_the_publish_entry_point_exits_with_the_sweep_status(monkeypatch):
    monkeypatch.setattr(server, "publish", lambda: 3)
    with pytest.raises(SystemExit) as info:
        server.run_publish()
    assert info.value.code == 3


# --- <cell>-db ------------------------------------------------------------------------------------


def test_db_migrate_reports_how_many_were_applied(monkeypatch, capsys):
    import cellkit.db

    seen: list[Any] = []
    monkeypatch.setattr(cellkit.db, "default_cluster", lambda spec: "cluster-sentinel")
    monkeypatch.setattr(
        cellkit.db,
        "migrate",
        lambda cluster: seen.append(cluster) or ["0001_kit_sample.sql"],
    )
    assert server.db(["migrate"]) == 0
    assert seen == ["cluster-sentinel"]
    assert capsys.readouterr().out.strip() == "1 migrations applied"


def test_db_provision_defaults_the_project_to_the_repository_name(
    monkeypatch, tmp_path, capsys
):
    import cellkit.db

    repo = tmp_path / "kit-sample-repo"
    repo.mkdir()
    shutil.copy(FIXTURE_SPEC, repo / "cell.yaml")
    monkeypatch.setenv("CELL_APP_ROOT", str(repo))
    provisioned: list[tuple[str, str]] = []

    def fake_provision(spec, project, region):
        provisioned.append((project, region))
        return "proj-123"

    monkeypatch.setattr(cellkit.db, "provision", fake_provision)
    assert server.db(["provision"]) == 0
    assert provisioned == [("kit-sample-repo", "aws-ap-southeast-1")]
    out = capsys.readouterr().out
    assert out.startswith("neon project proj-123 ready; URLs in ")
    assert "owner-only" in out


def test_db_provision_takes_the_project_and_region_it_is_given(monkeypatch):
    import cellkit.db

    provisioned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cellkit.db,
        "provision",
        lambda spec, project, region: provisioned.append((project, region)) or "p",
    )
    server.db(["provision", "--project", "other", "--region", "aws-us-east-1"])
    assert provisioned == [("other", "aws-us-east-1")]


def test_db_without_a_command_is_a_usage_error():
    with pytest.raises(SystemExit):
        server.db([])


def test_the_db_entry_point_exits_with_the_command_status(monkeypatch):
    monkeypatch.setattr(server, "db", lambda: 0)
    with pytest.raises(SystemExit) as info:
        server.run_db()
    assert info.value.code == 0
