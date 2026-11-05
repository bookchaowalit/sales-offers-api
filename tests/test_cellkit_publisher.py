"""Characterizes cellkit.publisher: the outbox sweep per workspace, backoff, batching and the one-report-per-sweep rule."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import cellkit_support as support
import jwt
import pytest
from cellkit_support import TEST_KEY, in_workspace, publisher_token
from psycopg.types.json import Jsonb

from cellkit import publisher
from cellkit.spec import Cell


class FakePlatform:
    """Accepts every event except subjects named ``bad``; records what it was given."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, token: str, event: dict[str, Any]) -> str:
        if event["subject"].startswith("bad"):
            raise ConnectionError("platform says no")
        self.events.append(event)
        return "applied"


def enqueue(cluster, cell: Cell, ws: uuid.UUID, subject: str, **over: Any) -> int:
    values: dict[str, Any] = {
        "type": "kit-sample.widget.created",
        "subject": subject,
        "payload": Jsonb(over.pop("payload", {})),
        "execution_id": over.pop("execution_id", None),
        "correlation_id": over.pop("correlation_id", None),
    }
    values.update(over)
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    [(row_id,)] = in_workspace(
        cluster,
        cell.role("api"),
        ws,
        f"INSERT INTO {cell.schema}.outbox (workspace_id, {columns})"
        f" VALUES (current_setting('app.workspace_id')::uuid, {placeholders}) RETURNING id",
        tuple(values.values()),
    )
    return row_id


def sweep(
    cluster, cell: Cell, ws: uuid.UUID, platform, limit: int = 100
) -> dict[str, int]:
    with cluster.connect(cell.role("publisher")) as conn:
        return publisher.publish_pending(
            conn, cell.schema, platform, publisher_token(ws), limit=limit
        )


def row(cluster, cell: Cell, row_id: int) -> tuple[Any, ...]:
    with cluster.connect("postgres") as conn:
        return conn.execute(
            f"SELECT published_at IS NOT NULL, attempts, last_error,"
            f" extract(epoch FROM next_attempt_at - now()) FROM {cell.schema}.outbox WHERE id = %s",
            (row_id,),
        ).fetchone()


def test_the_token_names_the_workspace_it_publishes_for():
    ws = uuid.uuid4()
    assert publisher.token_workspace(publisher_token(ws)) == str(ws)
    # a token written with the id in upper case is normalized
    upper = jwt.encode({"ws": str(ws).upper()}, TEST_KEY, algorithm="HS256")
    assert publisher.token_workspace(upper) == str(ws)


def test_a_token_without_a_workspace_or_with_a_bad_one_is_refused():
    with pytest.raises(KeyError):
        publisher.token_workspace(jwt.encode({"sub": "x"}, TEST_KEY, algorithm="HS256"))
    with pytest.raises(ValueError, match="badly formed"):
        publisher.token_workspace(
            jwt.encode({"ws": "not-a-uuid"}, TEST_KEY, algorithm="HS256")
        )


def test_due_rows_of_the_token_workspace_are_published_with_their_envelope(
    mini_cell, mini_cluster
):
    ws, other = uuid.uuid4(), uuid.uuid4()
    execution = uuid.uuid4()
    first = enqueue(
        mini_cluster,
        mini_cell,
        ws,
        "widget-1",
        payload={"version": 2, "title": "A"},
        execution_id=execution,
        correlation_id="corr-abcd1234",
    )
    second = enqueue(mini_cluster, mini_cell, ws, "widget-2")
    enqueue(mini_cluster, mini_cell, other, "elsewhere")

    platform = FakePlatform()
    counts = sweep(mini_cluster, mini_cell, ws, platform)

    assert counts == {"published": 2, "failed": 0}
    assert [e["subject"] for e in platform.events] == ["widget-1", "widget-2"]
    event = platform.events[0]
    assert event["id"] == str(first)
    assert event["workspace_id"] == str(ws)
    assert event["type"] == "kit-sample.widget.created"
    assert event["version"] == 2
    assert event["classification"] == "confidential"
    assert event["payload"] == {"version": 2, "title": "A"}
    assert event["execution_id"] == str(execution)
    assert event["correlation_id"] == "corr-abcd1234"
    assert datetime.fromisoformat(event["occurred_at"]).tzinfo is not None
    assert platform.events[1]["execution_id"] is None
    assert platform.events[1]["version"] == 1
    assert row(mini_cluster, mini_cell, first)[:2] == (True, 1)
    assert row(mini_cluster, mini_cell, second)[:2] == (True, 1)


def test_a_published_row_is_not_sent_again(mini_cell, mini_cluster):
    ws = uuid.uuid4()
    enqueue(mini_cluster, mini_cell, ws, "once")
    platform = FakePlatform()
    assert sweep(mini_cluster, mini_cell, ws, platform)["published"] == 1
    assert sweep(mini_cluster, mini_cell, ws, platform) == {"published": 0, "failed": 0}
    assert [e["subject"] for e in platform.events] == ["once"]


def test_a_batch_limit_leaves_the_rest_for_the_next_sweep(mini_cell, mini_cluster):
    ws = uuid.uuid4()
    for n in range(3):
        enqueue(mini_cluster, mini_cell, ws, f"batch-{n}")
    platform = FakePlatform()
    assert sweep(mini_cluster, mini_cell, ws, platform, limit=2) == {
        "published": 2,
        "failed": 0,
    }
    assert sweep(mini_cluster, mini_cell, ws, platform, limit=2) == {
        "published": 1,
        "failed": 0,
    }
    assert [e["subject"] for e in platform.events] == ["batch-0", "batch-1", "batch-2"]


def test_a_failed_row_backs_off_and_is_reported_once_per_sweep(
    mini_cell, mini_cluster, monkeypatch
):
    ws = uuid.uuid4()
    reports: list[BaseException] = []
    monkeypatch.setattr(publisher, "capture", reports.append)
    bad_one = enqueue(mini_cluster, mini_cell, ws, "bad-1")
    bad_two = enqueue(mini_cluster, mini_cell, ws, "bad-2")
    good = enqueue(mini_cluster, mini_cell, ws, "good")

    platform = FakePlatform()
    counts = sweep(mini_cluster, mini_cell, ws, platform)

    assert counts == {"published": 1, "failed": 2}
    assert [e["subject"] for e in platform.events] == ["good"]
    assert len(reports) == 1, "one Sentry report per sweep, not one per failed row"
    assert isinstance(reports[0], ConnectionError)

    published, attempts, last_error, seconds_left = row(
        mini_cluster, mini_cell, bad_one
    )
    assert (published, attempts, last_error) == (False, 1, "ConnectionError")
    assert 3 < seconds_left <= 5, "first retry after 5 seconds"
    assert row(mini_cluster, mini_cell, good)[:2] == (True, 1)

    # not due yet: the next sweep does not touch the failed rows
    assert sweep(mini_cluster, mini_cell, ws, platform) == {"published": 0, "failed": 0}
    assert row(mini_cluster, mini_cell, bad_two)[1] == 1


def test_the_retry_delay_doubles_and_stops_at_ten_minutes(
    mini_cell, mini_cluster, monkeypatch
):
    ws = uuid.uuid4()
    monkeypatch.setattr(publisher, "capture", lambda exc: None)
    many = enqueue(mini_cluster, mini_cell, ws, "bad-many", attempts=20)
    sweep(mini_cluster, mini_cell, ws, FakePlatform())
    _, attempts, _, seconds_left = row(mini_cluster, mini_cell, many)
    assert attempts == 21
    assert 595 < seconds_left <= 600, (
        "2**10 * 5 seconds is capped at MAX_BACKOFF_SECONDS"
    )


def test_a_retried_row_that_succeeds_clears_its_error(
    mini_cell, mini_cluster, monkeypatch
):
    ws = uuid.uuid4()
    monkeypatch.setattr(publisher, "capture", lambda exc: None)
    row_id = enqueue(mini_cluster, mini_cell, ws, "bad-then-fine")
    sweep(mini_cluster, mini_cell, ws, FakePlatform())
    assert row(mini_cluster, mini_cell, row_id)[2] == "ConnectionError"

    with mini_cluster.connect("postgres") as conn:
        conn.execute(
            f"UPDATE {mini_cell.schema}.outbox SET next_attempt_at = now() WHERE id = %s",
            (row_id,),
        )
        conn.commit()

    class Accepting(FakePlatform):
        def publish(self, token: str, event: dict[str, Any]) -> str:
            self.events.append(event)
            return "applied"

    assert sweep(mini_cluster, mini_cell, ws, Accepting()) == {
        "published": 1,
        "failed": 0,
    }
    published, attempts, last_error, _ = row(mini_cluster, mini_cell, row_id)
    assert (published, attempts, last_error) == (True, 2, None)


mini_root = support.mini_root
mini_cell = support.mini_cell
mini_cluster = support.mini_cluster
