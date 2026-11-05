"""Characterizes cellkit.importer: SQLite-to-API import conversion, ordering, rerun rules and the https/loopback check."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cellkit_support as support
import httpx
import pytest

from cellkit import importer
from cellkit.spec import Field

FIXTURE_DIR = Path(__file__).parent / "cellkit_fixture"
TOKEN = "tok-SECRET-do-not-print-123456"

OLD_WIDGETS = (
    "CREATE TABLE old_widgets (id INTEGER PRIMARY KEY, title TEXT, status INTEGER,"
    " category TEXT, quantity INTEGER, weight REAL, active INTEGER, starts_on TEXT,"
    " ends_on TEXT, meta TEXT, tags TEXT, seen_at TEXT, parent_id INTEGER, code TEXT,"
    " created_at TEXT, updated_at TEXT)"
)
OLD_PARTS = "CREATE TABLE parts (id INTEGER PRIMARY KEY, name TEXT, widget_id INTEGER, qty INTEGER)"


def field(name: str, kind: str, **extra: Any) -> Field:
    return Field(name=name, type=kind, **extra)


def old_db(path: Path, widgets: list[tuple], parts: list[tuple] | None = None) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(OLD_WIDGETS)
    conn.execute(OLD_PARTS)
    conn.executemany(
        "INSERT INTO old_widgets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", widgets
    )
    conn.executemany("INSERT INTO parts VALUES (?,?,?,?)", list(parts or []))
    conn.commit()
    conn.close()
    return path


def widget_row(i: int, **over: Any) -> tuple:
    values: dict[str, Any] = {
        "id": i,
        "title": f"W{i}",
        "status": 1,
        "category": "alpha",
        "quantity": 5,
        "weight": 2.5,
        "active": 1,
        "starts_on": "2026-10-01",
        "ends_on": "2026-10-31",
        "meta": '{"n": 1}',
        "tags": "[1, 2]",
        "seen_at": "2026-10-01 08:00:00",
        "parent_id": None,
        "code": f"C{i}",
        "created_at": "2026-10-01 08:00:00",
        "updated_at": None,
        **over,
    }
    return tuple(values.values())


def client_for(api: Api) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://api.example"
    )


class Api:
    """A fake cell API: records every request and answers from scripted rules."""

    def __init__(self, *, post_status: dict[int, int] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.posted: list[dict[str, Any]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.existing: set[int] = set()
        self.post_status = post_status or {}
        self.patch_status = 200
        self.list_pages: list[list[dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            legacy = body.get("legacy_id")
            status = self.post_status.get(legacy, 201)
            self.posted.append({"path": path, **body})
            if status == 409 and legacy in self.existing:
                return httpx.Response(409, json={"detail": "exists"})
            return httpx.Response(status, json={"id": legacy}, request=request)
        if request.method == "GET" and "/legacy/" in path:
            legacy = int(path.rsplit("/", 1)[1])
            code = 200 if legacy in self.existing else 404
            return httpx.Response(code, json={}, request=request)
        if request.method == "PATCH":
            self.patched.append((path, json.loads(request.content)))
            return httpx.Response(self.patch_status, json={}, request=request)
        if request.method == "GET":
            # only the widget list is scripted; any other list is empty
            after = int(request.url.params.get("after_id", "0"))
            scripted = path == "/v1/widgets" and bool(self.list_pages)
            page = self.list_pages[after // 1000] if scripted else []
            return httpx.Response(200, json={"items": page}, request=request)
        return httpx.Response(405, request=request)


@pytest.fixture
def api_calls(monkeypatch):
    """Route every httpx.Client the importer builds to a MockTransport; yields the Api for the test to script."""
    holder: dict[str, Api] = {}
    real = httpx.Client

    def install(api: Api) -> Api:
        holder["api"] = api

        def make(**kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(api), **kwargs)

        monkeypatch.setattr(httpx, "Client", make)
        return api

    return install


# --- value conversion ------------------------------------------------------------------------------


def test_bool_accepts_sqlite_zero_one_and_other_truthy_values():
    b = field("active", "bool")
    assert importer.convert(b, 1) is True
    assert importer.convert(b, 0) is False
    assert importer.convert(b, "1") is True
    assert importer.convert(b, "0") is False
    assert importer.convert(b, "yes") is True
    assert importer.convert(b, None) is None


def test_timestamps_become_utc_iso_strings():
    ts = field("seen_at", "timestamp")
    assert importer.convert(ts, "2026-10-01 08:00:00") == "2026-10-01T08:00:00+00:00"
    # an offset that is already present is kept, not replaced with UTC
    assert (
        importer.convert(ts, "2026-10-01T08:00:00+07:00") == "2026-10-01T08:00:00+07:00"
    )
    assert importer.stamp("2026-10-01 08:00:00") == "2026-10-01T08:00:00+00:00"
    assert importer.stamp(None) is None
    assert (
        importer.stamp(datetime(2026, 10, 1, tzinfo=UTC)) == "2026-10-01T00:00:00+00:00"
    )


def test_dates_keep_only_the_day_and_json_strings_are_parsed():
    assert (
        importer.convert(field("starts_on", "date"), "2026-10-01 00:00:00")
        == "2026-10-01"
    )
    meta = field("meta", "json", json_kind="object")
    assert importer.convert(meta, '{"n": 1}') == {"n": 1}
    assert importer.convert(meta, {"already": "parsed"}) == {"already": "parsed"}


def test_numbers_and_text_are_converted_to_their_field_type():
    assert importer.convert(field("qty", "int"), "7") == 7
    assert importer.convert(field("parent", "ref"), "3") == 3
    assert importer.convert(field("big", "bigint"), "9") == 9
    assert importer.convert(field("weight", "real"), "1.5") == 1.5
    assert importer.convert(field("title", "text"), "plain") == "plain"


# --- reading the old rows -------------------------------------------------------------------------


def test_rows_are_read_from_the_legacy_table_in_id_order(tmp_path, mini_cell):
    db = old_db(tmp_path / "old.sqlite", [widget_row(2), widget_row(1)])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = importer.rows_of(conn, mini_cell.resource("widget"))
    assert [row["id"] for row in rows] == [1, 2]
    assert rows[0]["title"] == "W1"


def test_parents_come_before_children_and_a_cycle_keeps_both(mini_cell):
    widget = mini_cell.resource("widget")
    rows = [
        {"id": 3, "parent_id": 2},
        {"id": 2, "parent_id": 1},
        {"id": 1, "parent_id": None},
    ]
    assert [row["id"] for row in importer.parents_first(widget, rows)] == [1, 2, 3]

    cycle = [{"id": 10, "parent_id": 11}, {"id": 11, "parent_id": 10}]
    assert sorted(row["id"] for row in importer.parents_first(widget, cycle)) == [
        10,
        11,
    ]

    # a parent that is not in the import is ignored, the row is still kept
    orphan = [{"id": 4, "parent_id": 99}]
    assert importer.parents_first(widget, orphan) == orphan


def test_resources_without_self_refs_keep_their_order(mini_cell):
    rows = [{"id": 2}, {"id": 1}]
    assert importer.parents_first(mini_cell.resource("part"), rows) is rows


def test_body_maps_legacy_ids_rewrites_values_and_defers_unknown_self_refs(mini_cell):
    widget = mini_cell.resource("widget")
    row = dict(
        zip(
            [
                "id",
                "title",
                "status",
                "category",
                "quantity",
                "weight",
                "active",
                "starts_on",
                "ends_on",
                "meta",
                "tags",
                "seen_at",
                "parent_id",
                "code",
                "created_at",
                "updated_at",
            ],
            widget_row(7, status=0, parent_id=5, updated_at="2026-10-02 09:00:00"),
            strict=True,
        )
    )
    body, later = importer.body_of(widget, row, created=set())
    assert body["legacy_id"] == 7
    assert body["status"] == "draft"  # legacy_values: 0 -> draft
    assert body["active"] is True
    assert body["meta"] == {"n": 1}
    assert body["tags"] == [1, 2]
    assert body["starts_on"] == "2026-10-01"
    assert body["created_at"] == "2026-10-01T08:00:00+00:00"
    assert body["updated_at"] == "2026-10-02T09:00:00+00:00"
    assert "parent_id" not in body
    assert later == {"parent_id": 5}

    body, later = importer.body_of(widget, row, created={5})
    assert body["parent_id"] == 5
    assert later == {}


def test_body_leaves_out_nulls_and_missing_columns(mini_cell):
    part = mini_cell.resource("part")
    body, later = importer.body_of(
        part, {"id": 4, "name": "bolt", "widget_id": 3, "qty": None}, set()
    )
    assert body == {"legacy_id": 4, "name": "bolt", "widget_id": 3}
    assert later == {}
    # a column the old table does not have is simply not sent
    body, _ = importer.body_of(part, {"id": 5, "name": "nut", "widget_id": 3}, set())
    assert body == {"legacy_id": 5, "name": "nut", "widget_id": 3}


# --- running the import against a fake API ---------------------------------------------------------


def test_import_creates_parents_first_and_keeps_old_ids(tmp_path, mini_cell, api_calls):
    api = api_calls(Api())
    db = old_db(
        tmp_path / "old.sqlite",
        [
            widget_row(3, parent_id=2),
            widget_row(1, status=1),
            widget_row(2, parent_id=1, status=0),
        ],
        [(1, "bolt", 3, 2)],
    )
    report = importer.run(
        mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
    )

    assert report == {
        "widget": {"rows": 3, "created": 3, "existing": 0, "linked": 0},
        "part": {"rows": 1, "created": 1, "existing": 0, "linked": 0},
    }
    assert [p["legacy_id"] for p in api.posted if p["path"] == "/v1/widgets"] == [
        1,
        2,
        3,
    ]
    assert api.posted[1]["status"] == "draft"
    assert api.posted[1]["parent_id"] == 1
    assert [p["path"] for p in api.posted] == ["/v1/widgets"] * 3 + ["/v1/parts"]
    assert api.patched == []


def test_dry_run_reads_and_counts_without_calling_the_api(
    tmp_path, mini_cell, api_calls
):
    api = api_calls(Api())
    db = old_db(tmp_path / "old.sqlite", [widget_row(1), widget_row(2)])
    report = importer.run(
        mini_cell,
        db,
        "https://api.example",
        TOKEN,
        dry_run=True,
        client=client_for(api),
    )
    assert report["widget"] == {"rows": 2, "created": 0, "existing": 0, "linked": 0}
    assert api.requests == []


def test_a_dry_run_of_a_cycle_sends_no_second_pass(tmp_path, mini_cell, api_calls):
    api = api_calls(Api())
    db = old_db(
        tmp_path / "old.sqlite",
        [widget_row(10, parent_id=11), widget_row(11, parent_id=10)],
    )
    report = importer.run(
        mini_cell,
        db,
        "https://api.example",
        TOKEN,
        dry_run=True,
        client=client_for(api),
    )
    assert report["widget"] == {"rows": 2, "created": 0, "existing": 0, "linked": 0}
    assert api.requests == []


def test_a_rerun_counts_rows_that_already_exist(tmp_path, mini_cell, api_calls):
    api = Api(post_status={2: 409})
    api.existing = {2}
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(1), widget_row(2)])
    report = importer.run(
        mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
    )
    assert report["widget"] == {"rows": 2, "created": 1, "existing": 1, "linked": 0}


def test_a_conflict_that_is_not_an_existing_row_stops_the_import(
    tmp_path, mini_cell, api_calls
):
    api = Api(post_status={1: 409})  # 409 but the legacy id does not answer 200
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    with pytest.raises(SystemExit, match=r"widget legacy_id=1: HTTP 409"):
        importer.run(
            mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
        )


def test_an_unexpected_status_stops_the_import_with_its_text(tmp_path, mini_cell):
    api = Api(post_status={1: 500})
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    with pytest.raises(SystemExit, match=r"HTTP 500"):
        importer.run(
            mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
        )


def test_a_cycle_is_linked_in_a_second_pass(tmp_path, mini_cell, api_calls):
    api = api_calls(Api())
    db = old_db(
        tmp_path / "old.sqlite",
        [widget_row(10, parent_id=11), widget_row(11, parent_id=10)],
    )
    report = importer.run(
        mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
    )
    assert report["widget"] == {"rows": 2, "created": 2, "existing": 0, "linked": 1}
    # 11 is created first without its parent (10 is not there yet); 10 then points at the created 11
    assert [p["legacy_id"] for p in api.posted] == [11, 10]
    assert "parent_id" not in api.posted[0]
    assert api.posted[1]["parent_id"] == 11
    assert api.patched == [("/v1/widgets/11", {"parent_id": 10})]


def test_a_failed_second_pass_stops_the_import(tmp_path, mini_cell, api_calls):
    api = Api()
    api.patch_status = 500
    api_calls(api)
    db = old_db(
        tmp_path / "old.sqlite",
        [widget_row(10, parent_id=11), widget_row(11, parent_id=10)],
    )
    with pytest.raises(SystemExit, match=r"refs: HTTP 500"):
        importer.run(
            mini_cell, db, "https://api.example", TOKEN, client=client_for(api)
        )


def test_verify_counts_imported_and_missing_rows(tmp_path, mini_cell, api_calls):
    api = Api()
    api.list_pages = [[{"id": 1, "legacy_id": 1}, {"id": 3, "legacy_id": 3}]]
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(1), widget_row(2), widget_row(3)])
    report = importer.verify(mini_cell, db, "https://api.example", TOKEN)
    assert report["widget"] == {"old": 3, "imported": 2, "missing": 1}
    assert report["part"] == {"old": 0, "imported": 0, "missing": 0}


def test_verify_pages_through_more_than_one_page(tmp_path, mini_cell, api_calls):
    first = [{"id": i, "legacy_id": i} for i in range(1, 1001)]
    second = [{"id": 1001, "legacy_id": 1001}]
    api = Api()
    api.list_pages = [first, second]
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(i) for i in range(1, 1002)])
    report = importer.verify(mini_cell, db, "https://api.example", TOKEN)
    assert report["widget"] == {"old": 1001, "imported": 1001, "missing": 0}
    pages = [r for r in api.requests if r.url.path == "/v1/widgets"]
    assert [r.url.params["after_id"] for r in pages] == ["0", "1000"]


# --- the command line: https only off loopback, token read from a file, never printed ----------------


def run_cli(args: list[str], *, cell_dir: Path = FIXTURE_DIR) -> int:
    return importer.main([str(cell_dir), *args])


def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token.jwt"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    return path


def test_the_api_url_must_be_https_unless_it_is_loopback(tmp_path, mini_cell):
    db = old_db(tmp_path / "old.sqlite", [])
    for url in ("http://api.example.com", "ftp://127.0.0.1:9", "http://10.0.0.5:8000"):
        with pytest.raises(SystemExit, match="--api must be https"):
            run_cli(
                [
                    "--sqlite",
                    str(db),
                    "--api",
                    url,
                    "--token-file",
                    str(token_file(tmp_path)),
                ]
            )


def test_loopback_http_is_allowed_for_local_runs(tmp_path, capsys, api_calls):
    api_calls(Api())
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    code = run_cli(
        [
            "--sqlite",
            str(db),
            "--api",
            "http://127.0.0.1:9",
            "--token-file",
            str(token_file(tmp_path)),
            "--dry-run",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["widget"]["rows"] == 1


def test_main_never_prints_the_token(tmp_path, capsys, api_calls):
    api = api_calls(Api())
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    code = run_cli(
        [
            "--sqlite",
            str(db),
            "--api",
            "https://api.example",
            "--token-file",
            str(token_file(tmp_path)),
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    # the token went out only as the bearer of the API calls
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in api.requests)
    assert all(r.headers["X-Empire-Channel"] == "cli" for r in api.requests)


def test_verify_exits_nonzero_when_rows_are_missing(tmp_path, capsys, api_calls):
    api = Api()
    api.list_pages = [[]]
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    code = run_cli(
        [
            "--sqlite",
            str(db),
            "--api",
            "https://api.example",
            "--token-file",
            str(token_file(tmp_path)),
            "--verify",
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["widget"]["missing"] == 1


def test_verify_exits_zero_when_everything_is_imported(tmp_path, capsys, api_calls):
    api = Api()
    api.list_pages = [[{"id": 1, "legacy_id": 1}]]
    api_calls(api)
    db = old_db(tmp_path / "old.sqlite", [widget_row(1)])
    code = run_cli(
        [
            "--sqlite",
            str(db),
            "--api",
            "https://api.example",
            "--token-file",
            str(token_file(tmp_path)),
            "--verify",
        ]
    )
    assert code == 0


mini_root = support.mini_root
mini_cell = support.mini_cell
