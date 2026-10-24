# LoomSphere Engineering Instructions

## Project architecture

- LoomSphere is an AI software factory in development. The current application is a foundation, not the completed factory; see [the project README](../README.md).
- Backend: FastAPI, SQLAlchemy, Alembic, PostgreSQL, and Redis. Keep API, service, database, and schema responsibilities aligned with the existing backend structure.
- Frontend: Next.js, React, TypeScript, and Tailwind CSS. Follow existing application patterns and consult [frontend/AGENTS.md](../frontend/AGENTS.md) for Next.js-specific guidance; do not duplicate or override it here.
- Use TypeScript for frontend changes. Use `uv` and `pyproject.toml` for Python dependencies and tooling. Follow existing Docker and Docker Compose conventions when container changes are approved.

## Configuration and data

- Centralize backend settings in `backend/app/core/config.py` using `pydantic-settings`; do not read environment variables directly elsewhere in Python code.
- Put SQLAlchemy models in `backend/app/db/models/`. Manage schema changes with Alembic migrations; never modify production tables manually.
- Treat database contents as persistent unless a database is verified to be disposable. Before running migrations, migration tests, or tests that write, truncate, downgrade, or otherwise alter data, verify the effective database URL and confirm the target is isolated and disposable. If that cannot be confirmed, do not run the operation; report the limitation.

## Requirements and architecture

- Before implementation, consult the relevant PRD, design documents, architecture guidance, and ADRs.
- Report material discrepancies between requirements, approved architecture, documentation, and implementation.
- Do not silently change an approved architectural decision. Surface the evidence and obtain approval before implementing a deviation.
- Follow [ADR governance](../docs/decisions/README.md): proposed ADRs are not approval, and agents must not mark decisions accepted or superseded without explicit recorded human approval. Stop and request a human decision when requirements or constraints conflict with an accepted ADR.
- Include conformance evidence for accepted ADRs in implementation plans and reviews; link verification to the relevant tests or checks.
- Preserve existing application behavior, conventions, and uncommitted work. Do not overwrite unrelated user changes.
- Never hardcode secrets or expose them in output.

## Tests and validation

- Add or update tests for behavior changes. Pytest and Playwright are required backend and end-to-end validation for application changes; run them before reporting implementation complete when their prerequisites are safe and verified. If a required check cannot safely run, report the work as unverified rather than claiming it passed.
- For backend tests, work from `backend/` and use `uv run pytest -q`. Integration and migration tests require a dedicated disposable PostgreSQL database; inspect the test setup before choosing tests.
- For frontend checks, work from `frontend/`: `npm run lint`, `npm exec tsc -- --noEmit`, and `npm run build`.
- For end-to-end tests, work from the repository root and use `npx playwright test` only after verifying that the configured API and database are isolated and disposable.
- Never claim a test or check passed unless it was executed successfully. State the exact command and result; distinguish unrun checks and inferred outcomes from observed results.
