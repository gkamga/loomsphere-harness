# Feature Lifecycle

This is the original flow description. The canonical role names, gate
criteria, required records, and rework paths are defined in
[agent-execution-contracts.md](agent-execution-contracts.md), which supersedes
the role naming below ("DeepSeek Architect" → Architect, "Copilot Builder" →
Builder, with QA and Reviewer added before completion).

1. Create PRD

2. DeepSeek Architect
   - Architecture
   - Database Design
   - API Design
   - Test Plan

3. Human Approval

4. Copilot Builder
   - Models
   - Migration
   - API
   - Frontend
   - Tests

5. Validation

   Backend:
   uv run pytest

   Frontend:
   npm run lint
   npm run build

   E2E:
   npx playwright test

6. Feature Complete