"""This cell's service code; the entry points are in ``cellkit.server`` and ``cellkit.mcp``."""

from ._sentry import init_sentry as _init_sentry

_init_sentry()
