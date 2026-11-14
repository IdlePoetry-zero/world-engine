# World Engine development rules

- Read README.md and relevant docs/01 through docs/08 before implementation. R01-R18 remain the complete delivery scope. Milestones organize dependency order, not scope reduction.
- Root agent owns architecture, API integration, review, progress, and acceptance. Delegate bounded coding tasks to gpt-6-luna with max reasoning, fork_turns=none, as explicitly requested by the user.
- Work only in E:\AI-demo\world-engine unless a task explicitly requires read-only inspection of existing model services. Do not modify the previous game or novel-to-comic projects.
- Respect assigned file ownership. Coordinate interfaces before cross-module changes. Do not undo other agents' work.
- Store no provider secrets in source, logs, docs, test fixtures, browser storage, backups, or exports. Use secret references resolved from environment or OS credential storage.
- Backend namespace is world_engine under backend/. Domain is framework-independent. Providers live under backend/world_engine/adapters/models/. HTTP integration lives under backend/world_engine/api/. Frontend is React/TypeScript under frontend/.
- Root owns API contracts and integration. Frontend uses /api/v1 and typed request wrappers; never present mock data or rule-only responses as real model behavior.
- Every world mutation must be validated, scoped to world/branch, idempotent and transactionally committed. GM queries cannot alter world facts or simulation time. Narration cannot mutate authoritative state.
- Keep implementation tests meaningful: invariants, crash recovery, permissions, real API contracts, gameplay outcomes. Distinguish unit/stub checks from actual model and long-run acceptance.
- Consult primary upstream documentation and mature open-source implementations as needed, record adopted patterns and URLs; do not copy code without reviewing its license.
- Keep runtime environments, data, outputs and secrets outside tracked code in .runtime/ and .data/ as appropriate. Never download large model weights without an explicit need and verified destination.
- Use `.runtime/venv313/Scripts/python.exe` for backend verification. This project-local Python 3.13.18 runtime uses SQLite 3.53.1; the older `.runtime/venv` has SQLite 3.49.1 and is retained only as a bootstrap tool environment. Do not use the older runtime for product or long-run acceptance.
- Track progress in docs/development/. Do not claim complete based on scaffolding, design text, elapsed time or unverified tests.
