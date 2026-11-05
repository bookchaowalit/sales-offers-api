# SPEC-002 — Test depth for the cellkit copy (characterization)

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-002 / 1.0 |
| Status | implemented |
| Repository / path | bookchaowalit/sales-offers-api / `specs/002-cellkit-test-depth/spec.md` |
| Owner (maker) | Claude Code |
| Reviewer (checker) | Codex review Stop hook + a fresh Claude reviewer |
| Approver | Owner in chat 2026-11-05 ("ทำต่อเลย", after the test-depth audit proposed raising tests on the 18 thin cell APIs) |
| Origin | Ported unchanged from bookchaowalit/finance-budgets-api SPEC-002 (`bb08761`). This repo's `src/cellkit` matches that copy apart from the `__init__.py` docstring. |
| Main-only | yes |
| Created | 2026-11-05 |

## 1. Problem and intent

- **Problem:** the CI gate passed with ≥ 3 test files, but `src/cellkit` (importer, MCP adapter, entry points, SQL generator, platform client, outbox publisher, DB tool) had little or no coverage. Total coverage was 60.50%.
- **Outcome:** characterization tests for those modules, written against a small self-contained spec (`tests/cellkit_fixture/cell.yaml`), so they never depend on this cell's own `cell.yaml`. Nothing under `src/` changed.

## 2. Scope

| In scope | Out of scope |
|---|---|
| `tests/test_cellkit_{sqlgen,importer,mcp,server,platform,publisher,db}.py`, `tests/cellkit_support.py`, `tests/cellkit_fixture/`, CI coverage ratchet | Any change under `src/`. That includes the bug below, which needs its own red/green change |

## 3. TDD note

These are characterization tests of existing code with no behaviour change, so there is no red run. One known bug is pinned as a strict xfail: `tests/test_cellkit_platform.py::test_a_refusal_with_a_non_json_body_is_still_a_denial`. When the platform (or a gateway in front of it) returns a 403 with a non-JSON body, the client gets a 500 instead of a refusal. Nothing is written either way. A fix must remove the xfail, and that test will be its red run.

## 4. Evidence (2026-11-05)

| Measure | Before | After |
|---|---|---|
| Total coverage | 60.50% | 93.67% |
| CI `coverage-threshold` | 57 | 91 (measured − 2, rounded down) |

- `.venv/bin/pytest -q --cov` run twice, both green: `236 passed, 11 skipped, 1 xfailed, 1 warning in 29.46s`
- `uvx ruff@0.17.1 check .`, `uvx ruff@0.17.1 format --check .` and `.venv/bin/mypy` all pass.
