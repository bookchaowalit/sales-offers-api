"""Shared fixtures for the cellkit characterization tests: a mini cell and a throwaway PostgreSQL.

The mini cell is ``tests/cellkit_fixture/cell.yaml`` copied into a temporary root, with its
migration and roles SQL generated from it. Nothing here reads this cell's own ``cell.yaml``,
schema or resources, so these files can be copied unchanged into any cell that shares ``src/cellkit``.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest

from cellkit.db import LocalCluster, Names, migrate
from cellkit.spec import Cell, load
from cellkit.sqlgen import migration, roles

TEST_KEY = "test-only-signing-key-0123456789abcdef"  # HMAC for test-shaped tokens; never verified by cellkit
FIXTURE_SPEC = Path(__file__).parent / "cellkit_fixture" / "cell.yaml"


def free_port() -> int:
    """A loopback port nobody is listening on; the harness cluster keeps its own fixed port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_mini_cell(root: Path) -> Cell:
    """Copy the fixture spec into ``root`` and generate its roles and first migration there."""
    shutil.copy(FIXTURE_SPEC, root / "cell.yaml")
    cell = load(root / "cell.yaml")
    (root / "db").mkdir()
    (root / "db" / "roles.sql").write_text(roles(cell), encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_kit_sample.sql").write_text(
        migration(cell), encoding="utf-8"
    )
    return cell


@pytest.fixture(scope="session")
def mini_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="ck-mini-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def mini_cell(mini_root: Path) -> Cell:
    return write_mini_cell(mini_root)


@dataclass
class PortedCluster(LocalCluster):
    """A throwaway server on its own port.

    LocalCluster reads the module-level PORT, which the harness cluster also uses. Patching that
    global for a session would leak into the harness, so this copy carries its own port instead.
    """

    port: int = 0

    def start(self) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = f"-c listen_addresses='' -k {self.socket_dir} -p {self.port}"
        self._run(
            "pg_ctl",
            "-D",
            str(self.data_dir),
            "-o",
            options,
            "-w",
            "-l",
            str(self.data_dir / "server.log"),
            "start",
        )

    def connect(
        self, role: str, dbname: str | None = None, **kwargs: Any
    ) -> psycopg.Connection[Any]:
        return psycopg.connect(
            f"host={self.socket_dir} port={self.port} user={role} dbname={dbname or self.names.database}",
            **kwargs,
        )


@pytest.fixture(scope="session")
def mini_cluster(mini_root: Path, mini_cell: Cell) -> Iterator[PortedCluster]:
    """A throwaway PostgreSQL on a free port, migrated with the mini cell's first migration."""
    cluster = PortedCluster(
        Names(mini_cell),
        mini_root / "pgdata",
        mini_root / "sock",
        port=free_port(),
    )
    cluster.init()
    try:
        cluster.start()
        cluster.bootstrap()
        migrate(cluster)
        yield cluster
    finally:
        cluster.stop()


def publisher_token(ws: uuid.UUID) -> str:
    """A token shaped like a publisher token; cellkit only reads its ``ws`` claim before the platform verifies it."""
    return jwt.encode({"ws": str(ws), "sub": "publisher"}, TEST_KEY, algorithm="HS256")


def in_workspace(
    cluster: LocalCluster,
    role: str,
    ws: uuid.UUID,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[tuple[Any, ...]]:
    """Run one statement as ``role`` inside a transaction bound to ``ws`` (row-level security applies)."""
    with cluster.connect(role) as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws),))
        cursor = conn.execute(query, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
    return rows
