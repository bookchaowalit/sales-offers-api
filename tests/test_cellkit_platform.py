"""Characterizes cellkit.platform: the https/loopback rule, the platform calls it makes, and how it fails closed."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from cellkit.platform import (
    Action,
    Denied,
    HttpPlatform,
    PlatformUnavailable,
    checked_url,
)

BASE = "https://platform.example"
TOKEN = "tok-SECRET-do-not-print-123456"


def platform_with(handler) -> HttpPlatform:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return HttpPlatform(BASE, client=client)


def record(
    calls: list[httpx.Request],
    status: int = 200,
    body: Any = None,
    *,
    raw: bytes | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if raw is not None:
            return httpx.Response(status, content=raw, request=request)
        return httpx.Response(
            status, json=body if body is not None else {}, request=request
        )

    return handler


ACTION = Action("kit-sample.widget:read", "widget", "42")


# --- the URL rule ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://platform.example",
        "https://platform.example/",
        "http://localhost:8000",
        "http://127.0.0.1:9000/base",
        "http://[::1]:8000",
    ],
)
def test_https_or_loopback_urls_are_accepted(url):
    assert checked_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://platform.example",
        "http://10.0.0.5",
        "ftp://platform.example",
        "platform.example",
    ],
)
def test_other_urls_are_refused_and_the_message_names_the_setting(url):
    with pytest.raises(ValueError, match="EMPIRE_PLATFORM_URL must be https"):
        checked_url(url)


def test_the_setting_name_can_be_given():
    with pytest.raises(ValueError, match="EMPIRE_OTHER_URL must be https"):
        checked_url("http://remote.example", "EMPIRE_OTHER_URL")


def test_the_platform_client_refuses_an_insecure_base_at_construction():
    with pytest.raises(ValueError, match="EMPIRE_PLATFORM_URL must be https"):
        HttpPlatform("http://platform.example")


def test_the_default_client_does_not_follow_redirects():
    assert HttpPlatform(BASE).client.follow_redirects is False


# --- authorization decisions ------------------------------------------------------------------------


def test_decide_forwards_the_callers_token_and_channel():
    calls: list[httpx.Request] = []
    platform = platform_with(record(calls, body={"decision": "allow"}))
    assert platform.decide(TOKEN, "mcp", ACTION) is True
    (request,) = calls
    assert request.url.path == "/v1/authz/decide"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Empire-Channel"] == "mcp"
    assert json.loads(request.content) == {
        "action": "kit-sample.widget:read",
        "resource_type": "widget",
        "resource_id": "42",
    }


def test_decide_is_true_only_for_an_allow():
    assert (
        platform_with(record([], body={"decision": "deny"})).decide(
            TOKEN, "api", ACTION
        )
        is False
    )
    assert platform_with(record([], body={})).decide(TOKEN, "api", ACTION) is False


def test_a_refused_token_is_reported_as_unauthenticated():
    platform = platform_with(record([], status=401, body={"detail": "bad"}))
    with pytest.raises(Denied) as info:
        platform.decide(TOKEN, "api", ACTION)
    assert (info.value.status, info.value.decision, info.value.reason) == (
        401,
        "unauthenticated",
        "platform_refused_token",
    )


def test_any_other_decision_status_fails_closed_as_unavailable():
    platform = platform_with(record([], status=500))
    with pytest.raises(PlatformUnavailable, match="decide 500"):
        platform.decide(TOKEN, "api", ACTION)


def test_redirects_are_not_followed():
    calls: list[httpx.Request] = []
    platform = platform_with(record(calls, status=302, body={}))
    with pytest.raises(PlatformUnavailable, match="decide 302"):
        platform.decide(TOKEN, "api", ACTION)
    assert len(calls) == 1


def test_a_transport_error_names_only_its_class_never_the_token():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach with {TOKEN}")

    with pytest.raises(PlatformUnavailable) as info:
        platform_with(down).decide(TOKEN, "api", ACTION)
    assert str(info.value) == "ConnectError"
    assert TOKEN not in str(info.value)


# --- executions -------------------------------------------------------------------------------------


def test_begin_sends_every_permission_and_the_idempotency_key_once():
    calls: list[httpx.Request] = []
    execution = uuid.uuid4()
    approval = uuid.uuid4()
    platform = platform_with(
        record(
            calls,
            status=201,
            body={"execution_id": str(execution), "replayed": True, "status": "done"},
        )
    )
    result = platform.begin(
        TOKEN,
        "api",
        Action("kit-sample.widget:write", "widget", "7", approval_id=approval),
        "key-0001-abcdef",
        "corr-1234",
        also=[
            "kit-sample.part:write",
            "kit-sample.widget:write",
            "kit-sample.part:write",
        ],
    )
    assert result == (execution, True, "done")
    body = json.loads(calls[0].content)
    assert body == {
        "action": "kit-sample.widget:write",
        "resource_type": "widget",
        "resource_id": "7",
        "approval_id": str(approval),
        "also": ["kit-sample.part:write", "kit-sample.widget:write"],
        "idempotency_key": "key-0001-abcdef",
        "correlation_id": "corr-1234",
    }
    assert calls[0].url.path == "/v1/executions"


def test_begin_without_optional_fields_sends_only_the_action():
    calls: list[httpx.Request] = []
    execution = uuid.uuid4()
    platform = platform_with(
        record(calls, status=201, body={"execution_id": str(execution)})
    )
    assert platform.begin(TOKEN, "api", ACTION, None, None) == (
        execution,
        False,
        "running",
    )
    assert json.loads(calls[0].content) == {
        "action": "kit-sample.widget:read",
        "resource_type": "widget",
        "resource_id": "42",
    }


@pytest.mark.parametrize("status", [401, 403, 409])
def test_a_refused_begin_carries_the_platforms_reason(status):
    platform = platform_with(
        record(
            [],
            status=status,
            body={"detail": {"error": "no_permission", "reason": "role"}},
        )
    )
    with pytest.raises(Denied) as info:
        platform.begin(TOKEN, "api", ACTION, None, None)
    assert (info.value.status, info.value.decision, info.value.reason) == (
        status,
        "no_permission",
        "role",
    )


def test_a_refused_begin_without_a_body_is_a_plain_deny():
    platform = platform_with(record([], status=403, raw=b""))
    with pytest.raises(Denied) as info:
        platform.begin(TOKEN, "api", ACTION, None, None)
    assert (info.value.status, info.value.decision, info.value.reason) == (
        403,
        "deny",
        "",
    )


def test_other_begin_statuses_fail_closed():
    platform = platform_with(record([], status=500))
    with pytest.raises(PlatformUnavailable, match="begin 500"):
        platform.begin(TOKEN, "api", ACTION, None, None)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a 403 from the platform or a gateway with a non-JSON body (e.g. an HTML page) raises "
    "json.JSONDecodeError; the API only maps Denied and PlatformUnavailable, so the caller gets a 500 "
    "instead of a refusal. Nothing is written either way.",
)
def test_a_refusal_with_a_non_json_body_is_still_a_denial():
    platform = platform_with(record([], status=403, raw=b"<html>Forbidden</html>"))
    with pytest.raises(Denied):
        platform.begin(TOKEN, "api", ACTION, None, None)


def test_finish_posts_the_outcome_to_the_execution():
    calls: list[httpx.Request] = []
    execution = uuid.uuid4()
    platform_with(record(calls, status=200, body={})).finish(
        TOKEN, "api", execution, {"status": "succeeded"}
    )
    assert calls[0].url.path == f"/v1/executions/{execution}/finish"
    assert json.loads(calls[0].content) == {"status": "succeeded"}


def test_a_failed_finish_is_unavailable_not_silent():
    platform = platform_with(record([], status=404, body={}))
    with pytest.raises(PlatformUnavailable, match="finish 404"):
        platform.finish(TOKEN, "api", uuid.uuid4(), {"status": "failed"})


# --- graph events ----------------------------------------------------------------------------------


def test_publish_returns_the_graph_outcome_and_uses_the_event_channel():
    calls: list[httpx.Request] = []
    outcome = platform_with(record(calls, body={"outcome": "applied"})).publish(
        TOKEN, {"id": "e1", "type": "kit-sample.widget.created"}
    )
    assert outcome == "applied"
    assert calls[0].url.path == "/v1/graph/events"
    assert calls[0].headers["X-Empire-Channel"] == "event"


def test_a_failed_publish_is_unavailable():
    with pytest.raises(PlatformUnavailable, match="publish 503"):
        platform_with(record([], status=503, body={})).publish(TOKEN, {"id": "e1"})


def test_a_transport_failure_during_publish_is_unavailable():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(PlatformUnavailable, match="ReadTimeout"):
        platform_with(down).publish(TOKEN, {"id": "e1"})
