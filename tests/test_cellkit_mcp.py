"""Characterizes cellkit.mcp: configuration checks, argument models, the tools' calls to the cell API, and their errors."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import cellkit_support as support
import httpx
import pydantic
import pytest
from cellkit_support import FIXTURE_SPEC
from mcp.server.mcpserver.exceptions import ToolError

from cellkit import mcp as kit_mcp
from cellkit.spec import parse

FIXTURE_DIR = FIXTURE_SPEC.parent
API = "https://api.example"
TOKEN = "tok-SECRET-do-not-print-123456"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read the developer's real client.env or token files: HOME and the client file point into tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMPIRE_CLIENT_CONFIG", str(tmp_path / "no-client.env"))
    monkeypatch.delenv("EMPIRE_TOKEN", raising=False)
    monkeypatch.delenv("EMPIRE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("EMPIRE_MCP_READONLY", raising=False)


def fixture_cell():
    from cellkit.spec import load

    return load(FIXTURE_SPEC)


def make_server(handler=None, *, read_only: bool = False, token=lambda: TOKEN):
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "id": 1}, request=request)

    inner = handler or default

    def record(request: httpx.Request) -> httpx.Response:
        if handler is not None:
            seen.append(request)
        return inner(request)

    client = httpx.Client(base_url=API, transport=httpx.MockTransport(record))
    server = kit_mcp.build_server(fixture_cell(), client, token, read_only=read_only)
    return server, seen


def call(server, name: str, arguments: dict[str, Any]):
    return asyncio.run(server.call_tool(name, arguments))


def status_reply(status: int, content: Any = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if content is None:
            return httpx.Response(status, request=request)
        return httpx.Response(status, json=content, request=request)

    return handler


# --- configuration -------------------------------------------------------------------------------


def test_the_api_variable_name_follows_the_cell_name():
    assert kit_mcp.api_key("kit-sample") == "EMPIRE_KIT_SAMPLE_API"


def test_the_api_url_comes_from_the_environment_as_given():
    url = kit_mcp.api_url(
        "kit-sample", {"EMPIRE_KIT_SAMPLE_API": " https://kit.example/ "}
    )
    assert url == "https://kit.example/"


@pytest.mark.parametrize(
    "value",
    [
        "http://kit.example",  # plain http to a remote host
        "ftp://kit.example",
        "https://",  # no host
    ],
)
def test_the_api_url_must_be_https_or_loopback(value):
    with pytest.raises(kit_mcp.ConfigError, match="must be https"):
        kit_mcp.api_url("kit-sample", {"EMPIRE_KIT_SAMPLE_API": value})


def test_loopback_http_is_the_only_plain_http_allowed():
    for value in (
        "http://127.0.0.1:3900",
        "http://localhost:3900",
        "http://[::1]:3900",
    ):
        assert kit_mcp.api_url("kit-sample", {"EMPIRE_KIT_SAMPLE_API": value}) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pw@kit.example",
        "https://kit.example?token=1",
        "https://kit.example/#frag",
        "https://kit.example:99999",
    ],
)
def test_userinfo_queries_fragments_and_bad_ports_are_refused(value):
    with pytest.raises(kit_mcp.ConfigError):
        kit_mcp.api_url("kit-sample", {"EMPIRE_KIT_SAMPLE_API": value})


def test_a_missing_api_setting_names_the_variable_only():
    with pytest.raises(kit_mcp.ConfigError, match="EMPIRE_KIT_SAMPLE_API"):
        kit_mcp.api_url("kit-sample", {})


def test_the_client_file_supplies_the_api_and_is_refused_unless_private(tmp_path):
    path = tmp_path / "client.env"
    path.write_text(
        "# written by hand\n\nEMPIRE_KIT_SAMPLE_API='https://from-file.example'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    environ = {"EMPIRE_CLIENT_CONFIG": str(path)}
    assert kit_mcp.api_url("kit-sample", environ) == "https://from-file.example"

    path.chmod(0o644)
    with pytest.raises(kit_mcp.ConfigError, match="chmod 600"):
        kit_mcp.api_url("kit-sample", environ)


def test_a_missing_client_file_is_not_an_error(tmp_path):
    assert (
        kit_mcp._client_file({"EMPIRE_CLIENT_CONFIG": str(tmp_path / "none.env")}) == {}
    )


def test_the_bearer_is_the_env_token_then_a_token_file(tmp_path):
    assert kit_mcp.bearer({"EMPIRE_TOKEN": f"  {TOKEN}\n"}) == TOKEN

    token = tmp_path / "caller.jwt"
    token.write_text(TOKEN + "\n", encoding="utf-8")
    token.chmod(0o600)
    assert kit_mcp.bearer({"EMPIRE_TOKEN_FILE": str(token)}) == TOKEN

    token.chmod(0o640)
    with pytest.raises(kit_mcp.ConfigError, match="readable by its owner only"):
        kit_mcp.bearer({"EMPIRE_TOKEN_FILE": str(token)})


def test_a_token_with_whitespace_or_non_ascii_is_refused_and_not_echoed():
    for bad in ("tok en", "tokén", "tok\x01en"):
        with pytest.raises(kit_mcp.ConfigError) as info:
            kit_mcp.bearer({"EMPIRE_TOKEN": bad})
        assert bad not in str(info.value)


def test_the_automation_token_is_found_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert kit_mcp.bearer({"EMPIRE_AUTOMATION_TOKEN_OWNER_MCP": TOKEN}) == TOKEN

    tokens = tmp_path / ".config" / "empire-platform" / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "ops-bot.jwt").write_text(TOKEN, encoding="utf-8")
    (tokens / "ops-bot.jwt").chmod(0o600)
    assert kit_mcp.bearer({"EMPIRE_MCP_AUTOMATION": "ops-bot"}) == TOKEN


def test_an_automation_name_must_be_short_and_lowercase():
    with pytest.raises(kit_mcp.ConfigError, match="short lowercase name"):
        kit_mcp.bearer({"EMPIRE_MCP_AUTOMATION": "Owner_MCP"})


def test_without_any_token_the_error_names_the_setting_not_a_value(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(kit_mcp.ConfigError) as info:
        kit_mcp.bearer({})
    assert "EMPIRE_AUTOMATION_TOKEN_OWNER_MCP" in str(info.value)


def test_a_group_readable_default_automation_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tokens = tmp_path / ".config" / "empire-platform" / "tokens"
    tokens.mkdir(parents=True)
    path = tokens / "owner-mcp.jwt"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(kit_mcp.ConfigError, match="readable by its owner only"):
        kit_mcp.bearer({})


# --- argument models ------------------------------------------------------------------------------


def test_list_arguments_are_the_declared_filters_plus_paging(mini_cell):
    models = kit_mcp.argument_models(mini_cell, mini_cell.resource("widget"))
    assert set(models["list"].model_fields) == {
        "status",
        "category",
        "limit",
        "offset",
        "order",
    }
    assert models["list"].model_validate({"limit": 200}).limit == 200
    with pytest.raises(pydantic.ValidationError):
        models["list"].model_validate({"limit": 201})
    with pytest.raises(pydantic.ValidationError):
        models["list"].model_validate({"title": "not a filter"})


def test_create_requires_required_fields_and_checks_bounds(mini_cell):
    models = kit_mcp.argument_models(mini_cell, mini_cell.resource("widget"))
    create = models["create"]
    assert "idempotency_key" in create.model_fields
    with pytest.raises(pydantic.ValidationError):
        create.model_validate({"code": "A-1"})  # title is required
    with pytest.raises(pydantic.ValidationError):
        create.model_validate({"title": "x", "quantity": 101})
    with pytest.raises(pydantic.ValidationError):
        create.model_validate({"title": "x", "status": "archived"})
    with pytest.raises(pydantic.ValidationError):
        create.model_validate({"title": "x", "idempotency_key": "short"})
    assert create.model_validate(
        {"title": "x", "idempotency_key": "key-0001-abcdef"}
    ).idempotency_key


def test_update_needs_a_positive_id_and_a_valid_expected_version(mini_cell):
    update = kit_mcp.argument_models(mini_cell, mini_cell.resource("widget"))["update"]
    with pytest.raises(pydantic.ValidationError):
        update.model_validate({"id": 0})
    with pytest.raises(pydantic.ValidationError):
        update.model_validate({"id": 1, "expected_version": 0})
    assert update.model_validate({"id": 1, "expected_version": 2}).expected_version == 2


def test_identity_like_fields_are_dropped_with_a_warning_and_a_caller_idempotency_key_is_kept():
    cell = parse(
        {
            "cell": "identity-check",
            "resources": {
                "note": {
                    "fields": {
                        "body": {"type": "text", "required": True},
                        "owner_id": {"type": "text"},
                        "idempotency_key": {"type": "text"},
                    }
                }
            },
        }
    )
    with pytest.warns(UserWarning, match="identity-like field is not exposed"):
        models = kit_mcp.argument_models(cell, cell.resource("note"))
    assert "owner_id" not in models["create"].model_fields
    assert "owner_id" not in models["update"].model_fields
    # the resource owns idempotency_key, so the tool does not add a second one
    assert "idempotency_key" in models["create"].model_fields


def test_tool_names_are_cell_resource_operation(mini_cell):
    widget = mini_cell.resource("widget")
    assert kit_mcp.tool_name(mini_cell, widget, "list") == "kit-sample.widget.list"


# --- the server -------------------------------------------------------------------------------------


def test_every_operation_is_registered_for_every_resource():
    server, _ = make_server()
    assert sorted(server.argument_models) == sorted(
        f"kit-sample.{resource}.{op}"
        for resource in ("widget", "part")
        for op in ("list", "get", "create", "update", "delete")
    )


def test_read_only_registers_list_and_get_only():
    server, _ = make_server(read_only=True)
    assert sorted(server.argument_models) == sorted(
        f"kit-sample.{resource}.{op}"
        for resource in ("widget", "part")
        for op in ("list", "get")
    )


def test_tools_refuse_unknown_arguments_and_name_only_the_fields():
    server, seen = make_server()
    with pytest.raises(ToolError, match="invalid_arguments: actor"):
        call(server, "kit-sample.widget.list", {"actor": str(uuid.uuid4())})
    with pytest.raises(ToolError, match="invalid_arguments: quantity") as info:
        call(server, "kit-sample.widget.create", {"title": "x", "quantity": 999})
    assert "999" not in str(info.value), "a bad value is never echoed back"
    assert seen == [], "no request is made for an invalid call"


def test_unknown_tool_names_are_refused():
    server, _ = make_server()
    with pytest.raises(ToolError, match=r"unknown tool kit-sample\.nothing\.list"):
        call(server, "kit-sample.nothing.list", {})


def test_list_tools_declares_no_extra_arguments():
    server, _ = make_server()
    tools = asyncio.run(server.list_tools())
    schema = {tool.name: tool.input_schema for tool in tools}[
        "kit-sample.widget.create"
    ]
    assert schema["additionalProperties"] is False
    assert "title" in schema["properties"]


def test_list_sends_only_the_filters_that_were_set():
    server, seen = make_server()
    call(server, "kit-sample.widget.list", {"status": "draft", "limit": 5})
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/v1/widgets"
    assert dict(request.url.params) == {
        "status": "draft",
        "limit": "5",
        "offset": "0",
        "order": "asc",
    }
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Empire-Channel"] == "mcp"
    assert request.headers["X-Correlation-Id"].startswith("mcp-")


def test_get_and_delete_address_one_record():
    server, seen = make_server()
    call(server, "kit-sample.widget.get", {"id": 3})
    call(server, "kit-sample.part.delete", {"id": 9})
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/widgets/3"),
        ("DELETE", "/v1/parts/9"),
    ]


def test_a_delete_with_no_body_reports_deleted():
    server, _ = make_server(status_reply(204))
    result = call(server, "kit-sample.part.delete", {"id": 9})
    assert json.loads(result.content[0].text) == {"deleted": True}


def test_create_sends_the_set_fields_and_a_generated_idempotency_key():
    server, seen = make_server()
    call(server, "kit-sample.widget.create", {"title": "Alpha", "quantity": 4})
    (request,) = seen
    assert json.loads(request.content) == {"title": "Alpha", "quantity": 4}
    assert request.headers["Idempotency-Key"].startswith("mcp-")


def test_create_uses_the_callers_idempotency_key_and_keeps_it_out_of_the_body():
    server, seen = make_server()
    call(
        server,
        "kit-sample.widget.create",
        {"title": "Alpha", "idempotency_key": "key-0001-abcdef"},
    )
    (request,) = seen
    assert request.headers["Idempotency-Key"] == "key-0001-abcdef"
    assert "idempotency_key" not in json.loads(request.content)


def test_update_sends_only_what_was_set_and_null_clears_a_field():
    server, seen = make_server()
    call(
        server,
        "kit-sample.widget.update",
        {"id": 3, "expected_version": 2, "status": "active"},
    )
    call(server, "kit-sample.widget.update", {"id": 3, "code": None})
    first, second = seen
    assert (first.method, first.url.path) == ("PATCH", "/v1/widgets/3")
    assert json.loads(first.content) == {"expected_version": 2, "status": "active"}
    assert json.loads(second.content) == {"code": None}


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "400: invalid"),
        (401, "401: unauthorized"),
        (403, "403: forbidden"),
        (404, "404: not_found"),
        (409, "409: conflict"),
        (412, "412: conflict"),
        (429, "429: rate_limited"),
        (418, "418: error"),
        (502, "502: upstream_error"),
    ],
)
def test_api_errors_reach_the_client_as_a_status_word(status, message):
    server, _ = make_server(
        status_reply(status, {"detail": "upstream text must not leak"})
    )
    with pytest.raises(ToolError, match=message) as info:
        call(server, "kit-sample.widget.get", {"id": 3})
    assert "upstream text" not in str(info.value)


def test_an_unreachable_api_is_reported_without_the_url_or_token():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {API} {TOKEN}")

    server, _ = make_server(down)
    with pytest.raises(ToolError, match="the cell API could not be reached") as info:
        call(server, "kit-sample.widget.get", {"id": 3})
    assert TOKEN not in str(info.value)
    assert API not in str(info.value)


def test_a_bad_token_at_call_time_is_a_tool_error_not_a_request():
    def no_token() -> str:
        raise kit_mcp.ConfigError(
            "the token is empty or contains whitespace, control or non-ASCII characters"
        )

    server, seen = make_server(token=no_token)
    with pytest.raises(ToolError, match="the token is empty"):
        call(server, "kit-sample.widget.get", {"id": 3})
    assert seen == []


def test_read_only_servers_still_refuse_writes_by_absence():
    server, _ = make_server(read_only=True)
    with pytest.raises(ToolError, match=r"unknown tool kit-sample\.widget\.delete"):
        call(server, "kit-sample.widget.delete", {"id": 3})


# --- entry point -------------------------------------------------------------------------------------


def test_find_cell_uses_the_app_root_variable(monkeypatch):
    monkeypatch.setenv("CELL_APP_ROOT", str(FIXTURE_DIR))
    assert kit_mcp.find_cell().name == "kit-sample"


def test_find_cell_fails_when_no_cell_yaml_is_anywhere(tmp_path, monkeypatch):
    monkeypatch.delenv("CELL_APP_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        kit_mcp, "__file__", str(tmp_path / "pkg" / "a" / "b" / "mcp.py")
    )
    with pytest.raises(SystemExit, match=r"cell\.yaml not found"):
        kit_mcp.find_cell()


def test_main_serves_stdio_with_the_configured_tools(monkeypatch):
    monkeypatch.setenv("CELL_APP_ROOT", str(FIXTURE_DIR))
    monkeypatch.setenv("EMPIRE_KIT_SAMPLE_API", "https://api.example")
    monkeypatch.setenv("EMPIRE_TOKEN", TOKEN)
    monkeypatch.setenv("EMPIRE_MCP_READONLY", "1")
    started: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        kit_mcp.StrictMCPServer,
        "run",
        lambda self, transport: started.append((self, transport)),
    )

    kit_mcp.main()

    ((server, transport),) = started
    assert transport == "stdio"
    assert sorted(server.argument_models) == sorted(
        f"kit-sample.{resource}.{op}"
        for resource in ("widget", "part")
        for op in ("list", "get")
    )


def test_main_refuses_to_start_without_configuration_and_never_prints_a_token(
    monkeypatch, capsys
):
    monkeypatch.setenv("CELL_APP_ROOT", str(FIXTURE_DIR))
    monkeypatch.delenv("EMPIRE_KIT_SAMPLE_API", raising=False)
    monkeypatch.setenv("EMPIRE_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="EMPIRE_KIT_SAMPLE_API"):
        kit_mcp.main()
    assert TOKEN not in capsys.readouterr().err


def test_main_builds_all_tools_when_not_read_only(monkeypatch):
    monkeypatch.setenv("CELL_APP_ROOT", str(FIXTURE_DIR))
    monkeypatch.setenv("EMPIRE_KIT_SAMPLE_API", "http://127.0.0.1:3900")
    monkeypatch.setenv("EMPIRE_TOKEN", TOKEN)
    monkeypatch.delenv("EMPIRE_MCP_READONLY", raising=False)
    started: list[Any] = []
    monkeypatch.setattr(
        kit_mcp.StrictMCPServer, "run", lambda self, transport: started.append(self)
    )
    kit_mcp.main()
    assert len(started[0].argument_models) == 10


mini_root = support.mini_root
mini_cell = support.mini_cell
