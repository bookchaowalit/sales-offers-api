"""The cell's MCP delete tools ask before they call the API (platform-cellkit SPEC-003).

The API is an httpx MockTransport, so no network or database is touched.
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


@pytest.fixture(autouse=True)
def ask_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the default mode, so a caller's EMPIRE_MCP_CONFIRM cannot change the outcome."""
    monkeypatch.setenv("EMPIRE_MCP_CONFIRM", "ask")


Callback = Any  # an mcp ElicitationFnT, or None for a client without forms


def answer(action: str, content: dict[str, Any] | None = None) -> Callback:
    async def callback(ctx: Any, params: Any) -> ElicitResult:
        return ElicitResult(action=action, content=content)  # type: ignore[arg-type]

    return callback


def run_delete(callback: Callback) -> tuple[str, list[tuple[str, str]]]:
    """Call the cell's first delete tool; return the reply text and the API calls made."""
    hits: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append((request.method, request.url.path))
        return httpx.Response(204)

    client = httpx.Client(
        base_url="http://cell.test", transport=httpx.MockTransport(handler)
    )
    server = build_server(load(ROOT / "cell.yaml"), client, lambda: "t" * 40)

    async def go() -> str:
        async with Client(server, elicitation_callback=callback) as c:
            tools = [
                t for t in (await c.list_tools()).tools if t.name.endswith(".delete")
            ]
            assert tools, "cell.yaml declares no deletable resource"
            schema = tools[0].input_schema["properties"]["id"]
            ident: Any = (
                1
                if schema.get("type") == "integer"
                else "00000000-0000-0000-0000-000000000001"
            )
            result = await c.call_tool(tools[0].name, {"id": ident})
            return " ".join(getattr(part, "text", "") for part in result.content)

    return anyio.run(go), hits


def test_yes_sends_one_delete() -> None:
    text, hits = run_delete(answer("accept", {"confirm": True}))
    assert [m for m, _ in hits] == ["DELETE"]
    assert "cancelled" not in text


@pytest.mark.parametrize(
    "callback",
    [
        answer("accept", {"confirm": False}),
        answer("decline"),
        answer("cancel"),
    ],
)
def test_no_sends_nothing(callback: Callback) -> None:
    text, hits = run_delete(callback)
    assert hits == []
    assert "cancelled: not confirmed" in text


def test_client_without_forms_keeps_old_behaviour() -> None:
    _, hits = run_delete(None)
    assert [m for m, _ in hits] == ["DELETE"]


def test_require_refuses_client_without_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMPIRE_MCP_CONFIRM", "require")
    text, hits = run_delete(None)
    assert hits == []
    assert "confirmation unavailable" in text
