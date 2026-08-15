# Injection APS repository rules

## Production safety (mandatory)

- This working tree is inside a production bench. `jce.1` is the production site and is read-only during development.
- Never run site-writing commands against `jce.1`, including migrate, patches, run-tests, execute/console mutations, install/uninstall, fixtures, scheduler actions, or direct SQL writes.
- Never run bench-wide build, restart, clear-cache, supervisor/systemd, or other process/asset/cache mutations while developing here.
- `jce-test.1` has a separate database but is not automatically authorized for writes. Use it only after the user explicitly approves the exact validation.
- Do not install Python/Node/system dependencies into this production bench for experiments.

## Existing customization safety (mandatory)

- Preserve existing Workspace, Custom HTML Block, Client Script, Property Setter, Customize Form layout, permissions, shortcuts, and Desk configuration.
- Existing same-name records are user data. Use create-if-missing; never overwrite, reorder, reset, or delete them from recurring install/migrate hooks.
- If Workspace JSON is invalid or ownership/customization is ambiguous, stop that resource update. Never replace it with an empty/default layout.
- Do not run formatters or bulk rewrites over unrelated files. Preserve all pre-existing dirty worktree changes.

## APS V2 execution

- Start at `docs/aps_v2/README.md` and follow `PRODUCTION_SAFETY.md`, `00_GLOBAL_CONTRACT.md`, `AI_EXECUTION_PROTOCOL.md`, and the current Phase file.
- Only one Phase may be `IN_PROGRESS`. Do not begin the next Phase until the current Phase has all required isolated migration, integration, permission, UI, and rollback evidence.
- Feature Flags fail closed. With V2 disabled, Legacy formal behavior must remain unchanged.
- Database fixtures, migrations, solver dependency validation, and UI tests require an isolated production-data copy that does not share production processes, assets, cache, queues, or database.
