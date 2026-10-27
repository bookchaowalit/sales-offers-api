"""Fixtures from the kit; the contract tests run against this cell's own cell.yaml."""

from pathlib import Path

import pytest

from cellkit.testing import client, harness, owner, platform  # noqa: F401


@pytest.fixture(scope="session")
def cell_root() -> Path:
    return Path(__file__).resolve().parents[1]
