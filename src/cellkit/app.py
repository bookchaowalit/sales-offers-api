"""The HTTP API of a cell, made from its spec.

The cell keeps its own database. Authorization, execution records and audit
live in empire-platform and are reached over HTTP with the caller's token:

- reads ask ``/v1/authz/decide`` (``<cell>.<resource>:read``);
- writes run a recorded precheck, change rows in an open local transaction,
  start a platform execution bound to the exact record and permission, commit,
  then finish the execution with row hashes.

Routes for every resource (``<plural>`` from the spec)::

    GET    /v1/<plural>                 list (field filters, id, legacy_id, updated_since,
                                        null, order, after_id, limit, offset)
    GET    /v1/<plural>/stats           counts grouped by one filter field
    GET    /v1/<plural>/legacy/{id}     one record by its id in the old system
    GET    /v1/<plural>/{id}            one record
    POST   /v1/<plural>                 create (Idempotency-Key; legacy_id keeps the old id)
    PATCH  /v1/<plural>/{id}            change fields (expected_version)
    DELETE /v1/<plural>/{id}            delete

Custom routes come from the spec's ``extend`` hook, which gets a ``Context``
with the same building blocks.
"""

import contextvars
import hashlib
import importlib
import json
import logging
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
)
from fastapi import (
    Request as HttpRequest,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as PydanticField

from cellkit._sentry import capture
from cellkit.auth import Principal, TokenVerifier, Unauthenticated
from cellkit.platform import Action, Denied, Platform, PlatformUnavailable
from cellkit.publisher import publish_pending
from cellkit.spec import LEGACY_LIMIT, Cell, Field, Resource

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
CORRELATION = re.compile(r"^[A-Za-z0-9-]{8,64}$")
STANDARD_PARAMS = {
    "limit",
    "offset",
    "order",
    "after_id",
    "id",
    "legacy_id",
    "updated_since",
    "null",
    "group_by",
}
MAX_LIMIT = 1000


# --- models from the spec ------------------------------------------------------------------------


def _python_type(f: Field) -> Any:
    if f.type == "enum":
        return Literal[f.values]
    return {
        "text": str,
        "int": int,
        "bigint": int,
        "ref": int,
        "real": float,
        "bool": bool,
        "date": date,
        "timestamp": datetime,
        "json": (
            list[Any]
            if f.json_kind == "array"
            else dict[str, Any]
            if f.json_kind == "object"
            else Any
        ),
    }[f.type]


# Integer column ranges (int = PostgreSQL integer, bigint and ref = bigint).
COLUMN_LIMITS = {
    "int": (-(2**31), 2**31 - 1),
    "bigint": (-(2**63), 2**63 - 1),
    "ref": (-(2**63), 2**63 - 1),
}


def _constraints(f: Field) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if f.type == "text":
        if f.max_length:
            out["max_length"] = f.max_length
        if f.required:
            out["min_length"] = 1
    if f.type in ("int", "bigint", "real"):
        if f.min is not None:
            out["ge"] = f.min
        if f.max is not None:
            out["le"] = f.max
    if f.type in COLUMN_LIMITS:
        # Past the column's range PostgreSQL raises (a 500); refuse it as a 422 instead.
        low, high = COLUMN_LIMITS[f.type]
        out["ge"] = max(out.get("ge", low), low)
        out["le"] = min(out.get("le", high), high)
    if f.type == "real":
        # JSON NaN/Infinity (or 1e400) would be stored as NULL, even in a required field.
        out["allow_inf_nan"] = False
    if f.type == "ref":
        out["gt"] = 0
    return out


def models(r: Resource) -> tuple[type[BaseModel], type[BaseModel]]:
    """(create model, update model): unknown fields are refused; required fields must be given on create."""
    config = ConfigDict(extra="forbid")
    create_fields: dict[str, Any] = {
        "legacy_id": (int | None, PydanticField(default=None, gt=0, lt=LEGACY_LIMIT)),
        # Only an import (legacy_id) may carry the old timestamps.
        "created_at": (datetime | None, PydanticField(default=None)),
        "updated_at": (datetime | None, PydanticField(default=None)),
    }
    update_fields: dict[str, Any] = {
        "expected_version": (int | None, PydanticField(default=None, ge=1))
    }
    for f in r.fields:
        typ, extra = _python_type(f), _constraints(f)
        if f.required and f.default is None:
            create_fields[f.name] = (typ, PydanticField(**extra))
        else:
            create_fields[f.name] = (typ | None, PydanticField(default=None, **extra))
        update_fields[f.name] = (typ | None, PydanticField(default=None, **extra))
    name = "".join(part.title() for part in r.name.split("-"))
    return (
        create_model(f"{name}Create", __config__=config, **create_fields),
        create_model(f"{name}Update", __config__=config, **update_fields),
    )


def coerce(f: Field | None, raw: str, name: str) -> Any:
    """A query-string value as the field's type (422 when it does not parse)."""
    try:
        if f is None or f.type in ("int", "bigint", "ref"):
            return int(raw)
        if f.type == "real":
            return float(raw)
        if f.type == "bool":
            if raw not in ("true", "false"):
                raise ValueError(raw)
            return raw == "true"
        if f.type == "date":
            return date.fromisoformat(raw)
        if f.type == "timestamp":
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: FURB162 - keeps Z-normalisation; removing it changes which inputs are accepted
        if f.type == "enum" and raw not in f.values:
            raise ValueError(raw)
        return raw
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_value", "fields": [name]}
        ) from None


def range_errors(
    r: Resource, row: dict[str, Any], touched: set[str] | None = None
) -> list[str]:
    """Fields of the resource's ranges whose start comes after their end (only pairs ``touched`` names)."""
    bad: set[str] = set()
    for start, end in r.ranges:
        if touched is not None and not touched & {start, end}:
            continue
        low, high = _ordered(row.get(start)), _ordered(row.get(end))
        if low is not None and high is not None and low > high:
            bad |= {start, end}
    return sorted(bad)


def _ordered(value: Any) -> Any:
    # A timestamp sent without an offset is read in the session time zone, UTC on the hosted
    # databases (Neon default); compare it as UTC.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def row_hash(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()


def out(row: dict[str, Any]) -> dict[str, Any]:
    encoded: dict[str, Any] = jsonable_encoder(row)
    return encoded


# --- the write path ----------------------------------------------------------------------------


@dataclass
class Work:
    """One write's open local transaction; ``marker`` tags its outbox rows until the execution exists."""

    conn: psycopg.Connection[Any]
    marker: uuid.UUID
    caller: Principal


@dataclass
class Plan:
    permission: str
    resource_id: str
    audit: dict[str, Any]
    result: Any
    # False when the result is a row this attempt inserted: a replay of a finished operation must not
    # return it (the insert is rolled back; the original row may have been deleted since).
    replay_safe: bool = True


@dataclass
class Context:
    """What a cell's ``extend`` hook gets to add routes that follow the same rules."""

    app: FastAPI
    cell: Cell
    caller: Any  # the FastAPI dependency type for the authenticated caller
    allow: Callable[..., None]
    bound: Callable[[Principal], Any]
    write: Callable[..., Any]
    load: Callable[..., dict[str, Any]]
    emit: Callable[..., dict[str, Any]]
    audit: Callable[..., dict[str, Any]]
    publish_soon: Callable[[BackgroundTasks, Principal], None]


def create_app(
    cell: Cell,
    connect: Callable[[], psycopg.Connection[Any]],
    verifier: TokenVerifier,
    platform: Platform,
    publisher_token: Callable[[], str | None] = lambda: None,
) -> FastAPI:
    app = FastAPI(title=f"{cell.title} API", version="1.0.0", description=__doc__)
    log = logging.getLogger(f"cell.{cell.name}")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: HttpRequest, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default 422 echoes each bad input; a NaN/Infinity input cannot be encoded as
        # JSON, which turned the 422 into a 500. Same body, with such inputs as strings.
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(_json_safe(exc.errors()))},
        )

    schema = cell.schema

    @app.middleware("http")
    async def correlate(
        request: HttpRequest, call_next: Callable[[HttpRequest], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get("x-correlation-id", "")
        token = correlation_id.set(
            given if CORRELATION.match(given) else uuid.uuid4().hex
        )
        try:
            response = await call_next(request)
        finally:
            value = correlation_id.get()
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = value
        return response

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_empire_channel: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return verifier.verify(authorization, x_empire_channel)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}
            ) from exc

    Caller = Annotated[Principal, Depends(principal)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection[Any]]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
        conn.row_factory = dict_row
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(caller.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def allow(
        caller: Principal,
        action: str,
        resource_type: str,
        resource_id: str = "*",
        *,
        not_found: bool = False,
    ) -> None:
        try:
            allowed = platform.decide(
                caller.token, caller.channel, Action(action, resource_type, resource_id)
            )
        except Denied as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthenticated", "reason": exc.reason},
            ) from exc
        except PlatformUnavailable as exc:
            raise HTTPException(
                status_code=503, detail={"error": "authorization_unavailable"}
            ) from exc
        if not allowed:
            if not_found:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(status_code=403, detail={"error": "deny"})

    def write(
        caller: Principal,
        resource_type: str,
        op: Callable[[Work], Plan],
        *,
        precheck: tuple[str, str],
        idempotency_key: str | None = None,
    ) -> Any:
        """Precheck, local change, platform execution bound to the exact record, commit, finish.

        A replay of the same operation (same idempotency key) continues a "running"
        execution whose answer was lost and returns the existing result for a
        finished one.
        """
        allow(caller, precheck[0], resource_type, precheck[1])
        with bound(caller) as conn:
            work = Work(conn, uuid.uuid4(), caller)
            try:
                plan = op(work)
                try:
                    execution, replayed, status = platform.begin(
                        caller.token,
                        caller.channel,
                        Action(plan.permission, resource_type, plan.resource_id),
                        idempotency_key,
                        correlation_id.get(),
                    )
                except Denied as exc:
                    code = exc.status if exc.status in (401, 403, 409) else 403
                    raise HTTPException(
                        status_code=code,
                        detail={"error": exc.decision, "reason": exc.reason},
                    ) from exc
                except PlatformUnavailable as exc:
                    raise HTTPException(
                        status_code=503, detail={"error": "authorization_unavailable"}
                    ) from exc
                if replayed and status == "succeeded":
                    conn.rollback()
                    if not plan.replay_safe:
                        raise HTTPException(
                            status_code=409, detail={"error": "replayed_record_gone"}
                        )
                    return plan.result
                if replayed and status != "running":
                    raise HTTPException(
                        status_code=409, detail={"error": "replayed", "status": status}
                    )
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                conn.execute(
                    f"UPDATE {schema}.outbox SET execution_id = %s WHERE execution_id = %s",
                    (execution, work.marker),
                )
                conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(
                    status_code=409, detail={"error": "conflict"}
                ) from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                if (exc.diag.message_primary or "").startswith(
                    "update or delete on table"
                ):
                    raise HTTPException(
                        status_code=409, detail={"error": "has_dependents"}
                    ) from exc
                raise HTTPException(
                    status_code=422, detail={"error": "reference_not_found"}
                ) from exc
            except (
                psycopg.errors.NotNullViolation,
                psycopg.errors.CheckViolation,
            ) as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_value"}
                ) from exc
        try:
            platform.finish(
                caller.token,
                caller.channel,
                execution,
                {"status": "succeeded", "service": cell.name, **plan.audit},
            )
        except (PlatformUnavailable, Denied) as exc:
            capture(exc)  # the write succeeded; only the audit finish failed
            log.warning(
                json.dumps(
                    {"event": f"{cell.name}.finish_failed", "execution": str(execution)}
                )
            )
        log.info(
            json.dumps(
                {
                    "event": f"{cell.name}.write",
                    "action": plan.permission,
                    "execution": str(execution),
                    "actor": str(caller.actor),
                    "workspace": str(caller.workspace),
                }
            )
        )
        return plan.result

    def audit(
        r: Resource,
        verb: str,
        record_id: int,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "action": f"{cell.name}.{r.name}.{verb}",
            "resource_type": r.name,
            "resource_id": str(record_id),
            "before_hash": row_hash(before),
            "after_hash": row_hash(after),
        }

    def emit(
        work: Work,
        r: Resource,
        verb: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current = after or before or {}
        if r.graph is not None:
            payload: dict[str, Any] = {}
            if after is not None:
                payload.update({name: after.get(name) for name in r.graph})
            payload.update(
                version=current.get("version", 0) + (1 if after is None else 0),
                deleted=after is None,
            )
            work.conn.execute(
                f"INSERT INTO {schema}.outbox (workspace_id, type, subject, execution_id, classification, payload,"
                " correlation_id) VALUES (%s, %s, %s, %s, 'confidential', %s, %s)",
                (
                    work.caller.workspace,
                    f"{cell.name}.{r.name}.{verb}",
                    f"empire:{cell.name}:{r.name}:{current['id']}",
                    work.marker,
                    json.dumps(payload, default=str),
                    correlation_id.get(),
                ),
            )
        return audit(r, verb, current["id"], before, after)

    def publish_soon(background: BackgroundTasks, caller: Principal) -> None:
        token = publisher_token()
        if not token or not cell.graph:
            return

        def run() -> None:
            try:
                with bound(caller) as conn:
                    publish_pending(conn, schema, platform, token, limit=20)
            except Exception as exc:  # noqa: BLE001 - the cron sweep retries; never fail the request for this
                capture(exc)
                log.warning(
                    json.dumps(
                        {
                            "event": f"{cell.name}.inline_publish_failed",
                            "error": type(exc).__name__,
                        }
                    )
                )

        background.add_task(run)

    def load(
        conn: psycopg.Connection[Any],
        r: Resource,
        record_id: int,
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
        row = conn.execute(
            f"SELECT {r.select} FROM {schema}.{r.table} WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (record_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        found: dict[str, Any] = row
        return found

    def reserve(caller: Principal, r: Resource, key: str) -> int:
        if not 8 <= len(key) <= 200:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_value", "fields": ["Idempotency-Key"]},
            )
        with bound(caller) as conn:
            conn.execute(
                f"INSERT INTO {schema}.create_keys (workspace_id, actor, kind, key, record_id)"
                f" VALUES (%s, %s, %s, %s, nextval(pg_get_serial_sequence('{schema}.{r.table}', 'id')))"
                " ON CONFLICT DO NOTHING",
                (caller.workspace, caller.actor, r.name, key),
            )
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            row = conn.execute(
                f"SELECT record_id FROM {schema}.create_keys WHERE actor = %s AND kind = %s AND key = %s",
                (caller.actor, r.name, key),
            ).fetchone()
            conn.commit()
        assert row is not None  # the INSERT above or an earlier request created it
        return int(row["record_id"])

    def where(r: Resource, query: Any) -> tuple[list[str], list[Any]]:
        """WHERE parts from the query string: field filters (repeat for any-of), id, legacy_id, updated_since, null."""
        clauses, params = ["TRUE"], []
        unknown = set(query.keys()) - STANDARD_PARAMS - set(r.filters)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_filter", "fields": sorted(unknown)},
            )
        for name in r.filters:
            raw = query.getlist(name)
            if raw:
                clauses.append(f"{name} = ANY(%s)")
                params.append([coerce(r.field(name), v, name) for v in raw])
        for name in ("id", "legacy_id"):
            raw = query.getlist(name)
            if raw:
                clauses.append(f"{name} = ANY(%s)")
                params.append([coerce(None, v, name) for v in raw])
        if query.get("updated_since"):
            clauses.append("updated_at >= %s")
            params.append(
                coerce(
                    Field("updated_since", "timestamp"),
                    query["updated_since"],
                    "updated_since",
                )
            )
        for name in query.getlist("null"):
            if name not in r.columns:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_value", "fields": ["null"]},
                )
            clauses.append(f"{name} IS NULL")
        return clauses, params

    def values(r: Resource, body: BaseModel, names: set[str]) -> dict[str, Any]:
        data = {f.name: getattr(body, f.name) for f in r.fields if f.name in names}
        nulls = sorted(
            f.name
            for f in r.fields
            if f.name in data and data[f.name] is None and not f.nullable
        )
        if nulls:
            raise HTTPException(
                status_code=422, detail={"error": "invalid_value", "fields": nulls}
            )
        return data

    def stored(r: Resource, data: dict[str, Any]) -> dict[str, Any]:
        return {
            k: (Jsonb(v) if r.field(k).type == "json" and v is not None else v)
            for k, v in data.items()
        }

    def changed(
        r: Resource, before: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        def same(name: str, value: Any) -> bool:
            if (
                r.field(name).type == "json"
            ):  # true and 1 are equal in Python, not in JSON
                return json.dumps(before.get(name), sort_keys=True) == json.dumps(
                    value, sort_keys=True
                )
            return bool(before.get(name) == value)

        return {name: value for name, value in data.items() if not same(name, value)}

    def register(r: Resource) -> None:
        create_body, update_body = models(r)
        table = f"{schema}.{r.table}"

        @app.get(f"/v1/{r.plural}", name=f"list_{r.plural}")
        def list_records(request: HttpRequest, caller: Caller) -> dict[str, Any]:
            query = request.query_params
            allow(caller, f"{cell.name}.{r.name}:read", r.name)
            clauses, params = where(r, query)
            limit = coerce(None, query.get("limit", "100"), "limit")
            offset = coerce(None, query.get("offset", "0"), "offset")
            order = query.get("order", "asc")
            if (
                not 1 <= limit <= MAX_LIMIT
                or offset < 0
                or order not in ("asc", "desc")
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_value", "fields": ["limit/offset/order"]},
                )
            direction = "DESC" if order == "desc" else "ASC"
            if query.get("after_id"):
                clauses.append("id < %s" if direction == "DESC" else "id > %s")
                params.append(coerce(None, query["after_id"], "after_id"))
            # A cursor (after_id) pages by id; otherwise the resource's own order, then id.
            sort = (
                f"{r.order} {direction}, id {direction}"
                if r.order != "id" and not query.get("after_id")
                else f"id {direction}"
            )
            with bound(caller) as conn:
                rows = conn.execute(
                    # nosemgrep: python.django.security.injection.tainted-sql-string.tainted-sql-string -- clauses/sort come from spec-validated column names with %s placeholders; values are parameters
                    f"SELECT {r.select} FROM {table} WHERE {' AND '.join(clauses)}"
                    f" ORDER BY {sort} LIMIT %s OFFSET %s",
                    [*params, limit, offset],
                ).fetchall()
            return {"items": [out(row) for row in rows]}

        @app.get(f"/v1/{r.plural}/stats", name=f"stats_{r.plural}")
        def stats(request: HttpRequest, caller: Caller) -> dict[str, Any]:
            group_by = request.query_params.get("group_by", "")
            if group_by not in r.filters:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_value", "fields": ["group_by"]},
                )
            allow(caller, f"{cell.name}.{r.name}:read", r.name)
            clauses, params = where(r, request.query_params)
            with bound(caller) as conn:
                rows = conn.execute(
                    # nosemgrep: python.django.security.injection.tainted-sql-string.tainted-sql-string -- group_by is checked against a fixed allow-list before use; values are %s parameters
                    f"SELECT {group_by} AS key, count(*) AS n FROM {table}"
                    f" WHERE {' AND '.join(clauses)} GROUP BY 1",
                    params,
                ).fetchall()
            return {"groups": {str(row["key"]): row["n"] for row in rows}}

        @app.get(f"/v1/{r.plural}/legacy/{{legacy_id}}", name=f"{r.name}_by_legacy_id")
        def by_legacy(legacy_id: int, caller: Caller) -> dict[str, Any]:
            allow(caller, f"{cell.name}.{r.name}:read", r.name, not_found=True)
            with bound(caller) as conn:
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                row = conn.execute(
                    f"SELECT {r.select} FROM {table} WHERE legacy_id = %s", (legacy_id,)
                ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            return out(row)

        @app.get(f"/v1/{r.plural}/{{record_id}}", name=f"get_{r.name}")
        def get_record(record_id: int, caller: Caller) -> dict[str, Any]:
            allow(
                caller,
                f"{cell.name}.{r.name}:read",
                r.name,
                str(record_id),
                not_found=True,
            )
            with bound(caller) as conn:
                return out(load(conn, r, record_id))

        @app.post(f"/v1/{r.plural}", status_code=201, name=f"create_{r.name}")
        def create(
            body: create_body,  # type: ignore[valid-type]  # model built from cell.yaml at startup
            caller: Caller,
            background: BackgroundTasks,
            idempotency_key: Annotated[str | None, Header()] = None,
        ) -> dict[str, Any]:
            # The body model is built from cell.yaml at startup, so mypy cannot see its fields.
            sent: Any = body
            data = {
                k: v
                for k, v in values(r, sent, sent.model_fields_set).items()
                if v is not None
            }
            stamps = {
                k: getattr(sent, k)
                for k in ("created_at", "updated_at")
                if getattr(sent, k) is not None
            }
            if stamps and sent.legacy_id is None:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_value", "fields": sorted(stamps)},
                )
            if bad := range_errors(r, data):
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_value", "fields": bad}
                )
            # Authorize before reserving anything (a reservation is a committed row).
            allow(caller, f"{cell.name}.{r.name}:write", r.name, "new")
            # An import keeps its old id (legacy_id); a retried import then conflicts instead of duplicating.
            reserved = (
                reserve(caller, r, idempotency_key)
                if idempotency_key and sent.legacy_id is None
                else None
            )

            def op(work: Work) -> Plan:
                if reserved is not None:
                    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                    existing = work.conn.execute(
                        f"SELECT {r.select} FROM {table} WHERE id = %s", (reserved,)
                    ).fetchone()
                    if existing is not None:
                        return Plan(
                            f"{cell.name}.{r.name}:write",
                            str(reserved),
                            audit(r, "created", reserved, None, existing),
                            existing,
                        )
                row_data = stored(r, data)
                columns, params = (
                    ["workspace_id", *row_data],
                    [caller.workspace, *row_data.values()],
                )
                if sent.legacy_id is not None:
                    columns.append("legacy_id")
                    params.append(sent.legacy_id)
                    for name, value in stamps.items():
                        columns.append(name)
                        params.append(value)
                record_id = sent.legacy_id if sent.legacy_id is not None else reserved
                if record_id is not None:
                    columns.append("id")
                    params.append(record_id)
                row = work.conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                    f" RETURNING {r.select}",
                    params,
                ).fetchone()
                assert row is not None  # INSERT ... RETURNING always returns the row
                return Plan(
                    f"{cell.name}.{r.name}:write",
                    str(row["id"]),
                    emit(work, r, "created", None, row),
                    row,
                    replay_safe=False,
                )

            row = write(
                caller,
                r.name,
                op,
                precheck=(f"{cell.name}.{r.name}:write", "new"),
                idempotency_key=idempotency_key,
            )
            publish_soon(background, caller)
            return out(row)

        @app.patch(f"/v1/{r.plural}/{{record_id}}", name=f"update_{r.name}")
        def update(
            record_id: int,
            body: update_body,  # type: ignore[valid-type]  # model built from cell.yaml at startup
            caller: Caller,
            background: BackgroundTasks,
        ) -> dict[str, Any]:
            # The body model is built from cell.yaml at startup, so mypy cannot see its fields.
            sent: Any = body
            requested = values(r, sent, sent.model_fields_set - {"expected_version"})

            def op(work: Work) -> Plan:
                before = load(work.conn, r, record_id, lock=True)
                if (
                    sent.expected_version is not None
                    and before["version"] != sent.expected_version
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "version_conflict",
                            "version": before["version"],
                        },
                    )
                data = changed(r, before, requested)
                # Only pairs this patch really changes: resending stored values is never refused.
                if bad := range_errors(r, {**before, **data}, set(data)):
                    raise HTTPException(
                        status_code=422,
                        detail={"error": "invalid_value", "fields": bad},
                    )
                if not data:
                    return Plan(
                        f"{cell.name}.{r.name}:write",
                        str(record_id),
                        audit(r, "unchanged", record_id, before, before),
                        before,
                    )
                row_data = stored(r, data)
                assignments = ", ".join(f"{name} = %s" for name in row_data)
                row = work.conn.execute(
                    f"UPDATE {table} SET {assignments}, version = version + 1, updated_at = now()"
                    f" WHERE id = %s RETURNING {r.select}",
                    [*row_data.values(), record_id],
                ).fetchone()
                return Plan(
                    f"{cell.name}.{r.name}:write",
                    str(record_id),
                    emit(work, r, "updated", before, row),
                    row,
                )

            row = write(
                caller,
                r.name,
                op,
                precheck=(f"{cell.name}.{r.name}:write", str(record_id)),
            )
            publish_soon(background, caller)
            return out(row)

        @app.delete(
            f"/v1/{r.plural}/{{record_id}}", status_code=204, name=f"delete_{r.name}"
        )
        def delete(record_id: int, caller: Caller, background: BackgroundTasks) -> None:
            def op(work: Work) -> Plan:
                before = load(work.conn, r, record_id, lock=True)
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                work.conn.execute(f"DELETE FROM {table} WHERE id = %s", (record_id,))
                return Plan(
                    f"{cell.name}.{r.name}:delete",
                    str(record_id),
                    emit(work, r, "deleted", before, None),
                    None,
                )

            write(
                caller,
                r.name,
                op,
                precheck=(f"{cell.name}.{r.name}:delete", str(record_id)),
            )
            publish_soon(background, caller)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if (
        cell.extend
    ):  # custom routes first, so a specific path wins over the generic /{id}
        module, _, function = cell.extend.partition(":")
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- module comes from the repo-owned cell.yaml extend key, never from a request
        hook = getattr(importlib.import_module(module), function or "register")
        hook(
            Context(
                app=app,
                cell=cell,
                caller=Caller,
                allow=allow,
                bound=bound,
                write=write,
                load=load,
                emit=emit,
                audit=audit,
                publish_soon=publish_soon,
            )
        )
    for r in cell.resources:
        register(r)
    return app
