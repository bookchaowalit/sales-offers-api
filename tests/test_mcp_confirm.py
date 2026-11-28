"""The cell's MCP delete tools ask before they call the API (platform-cellkit SPEC-003).

Every generated delete tool is driven in process against an httpx MockTransport API, so no
network or database is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from cellkit.mcp import build_server
from cellkit.spec import load
from mcp.client import Client
from mcp.types import ElicitResult

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40
Callback = Any  # an mcp ElicitationFnT, or None for a client without forms
asked: list[str] = []  # the questions the client was shown


@pytest.fixture(autouse=True)
def ask_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the default mode, so a caller's EMPIRE_MCP_CONFIRM cannot change the outcome."""
    monkeypatch.setenv("EMPIRE_MCP_CONFIRM", "ask")


def answer(action: str, content: dict[str, Any] | None = None) -> Callback:
    async def callback(ctx: Any, params: Any) -> ElicitResult:
        asked.append(params.message)
        return ElicitResult(action=action, content=content)  # type: ignore[arg-type]

    return callback


def run_deletes(callback: Callback) -> list[tuple[str, str, list[str]]]:
    """Call every delete tool once; return (tool, reply text, API methods) per tool."""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.method)
        return httpx.Response(204)

    client = httpx.Client(
        base_url="http://cell.test", transport=httpx.MockTransport(handler)
    )
    server = build_server(load(ROOT / "cell.yaml"), client, lambda: TOKEN)

    async def go() -> list[tuple[str, str, list[str]]]:
        out = []
        async with Client(server, elicitation_callback=callback) as c:
            tools = [
                t for t in (await c.list_tools()).tools if t.name.endswith(".delete")
            ]
            assert tools, "cell.yaml declares no deletable resource"
            for tool in tools:
                schema = tool.input_schema["properties"]["id"]
                ident: Any = (
                    1
                    if schema.get("type") == "integer"
                    else "00000000-0000-0000-0000-000000000001"
                )
                hits.clear()
                result = await c.call_tool(tool.name, {"id": ident})
                text = " ".join(getattr(part, "text", "") for part in result.content)
                out.append((tool.name, text, list(hits)))
        return out

    asked.clear()
    return anyio.run(go)


def test_yes_sends_one_delete_per_tool() -> None:
    results = run_deletes(answer("accept", {"confirm": True}))
    for name, text, hits in results:
        assert hits == ["DELETE"], name
        assert "cancelled" not in text, name
    assert len(asked) == len(results)
    for message in asked:
        assert "This cannot be undone." in message
        assert "\nWhere: " in message
        assert TOKEN not in message


@pytest.mark.parametrize(
    "callback",
    [
        answer("accept", {"confirm": False}),
        answer("decline"),
        answer("cancel"),
    ],
)
def test_no_sends_nothing(callback: Callback) -> None:
    for name, text, hits in run_deletes(callback):
        assert hits == [], name
        assert "cancelled: not confirmed" in text, name


def test_client_without_forms_keeps_old_behaviour() -> None:
    for name, _, hits in run_deletes(None):
        assert hits == ["DELETE"], name


def test_require_refuses_client_without_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMPIRE_MCP_CONFIRM", "require")
    for name, text, hits in run_deletes(None):
        assert hits == [], name
        assert "confirmation unavailable" in text, name
