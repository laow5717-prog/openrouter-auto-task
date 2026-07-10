# Implementation Plan

## Phase 1: Create package structure & data layer

- [x] 1.1 Create directory structure: `src/`, `src/models/`, `src/services/`, `src/browser/`, `src/api/`, `src/web/`, `data/`
- [x] 1.2 Add all `__init__.py` files
- [x] 1.3 Write `src/models/database.py` - Database class, schema creation, migration logic
- [x] 1.4 Write `src/models/account.py` - AccountModel (CRUD, upsert, list, search)
- [x] 1.5 Write `src/models/task.py` - TaskModel (create, update status, list)
- [x] 1.6 Write `src/models/card_binding.py` - CardBindingModel (CRUD, batch ops, summary)
- [x] 1.7 Write TXT migration logic in `database.py` (import registered_accounts.txt on first run)

**Validation:** PASSED - DB created, 15 accounts imported from TXT

## Phase 2: Move config & utils into src/

- [x] 2.1 Move `config.py` -> `src/config.py` (remove module-level constant exports, keep `cfg` object)
- [x] 2.2 Move `utils.py` -> `src/utils.py` (replace `save_to_txt`/`update_account_status` with DB calls)
- [x] 2.3 Add `database.path` to config dataclass with default `data/cloudflare_auto.db`

**Validation:** PASSED - `cfg.database.path` returns correctly

## Phase 3: Move business logic into services/

- [x] 3.1 Move `email_service.py` -> `src/services/email.py` (update imports)
- [x] 3.2 Move `captcha_solver.py` -> `src/services/captcha.py` (update imports)
- [x] 3.3 Move `card_manager.py` -> `src/services/card.py` (DB-backed, keep Excel parse/template)
- [x] 3.4 Move `browser.py` -> `src/browser/driver.py` (update imports to src.config, src.services.captcha)
- [x] 3.5 Move `main.py` logic -> `src/services/registration.py` (use DB models instead of TXT)

**Validation:** PASSED - all imports resolve

## Phase 4: Rebuild API & web layer

- [x] 4.1 Write `src/api/routes.py` - Flask Blueprint with all existing endpoints, using DB models
- [x] 4.2 Write `src/web/app.py` - `create_app()` factory, init DB, register blueprint, static serving
- [x] 4.3 Rewrite root `server.py` as thin entry point
- [x] 4.4 Move `AppState` to `src/web/app.py`, persist task results to DB

**Validation:** PASSED - All 10 routes registered, all 3 tested endpoints return correct data

## Phase 5: Cleanup & compatibility

- [x] 5.1 Old root-level files to be deleted (kept for now as backup until commit)
- [x] 5.2 Update `build.py` for new structure (add `--collect-submodules src`)
- [x] 5.3 Update `config.example.yaml` with `database.path` field
- [ ] 5.4 Update `pyproject.toml` if needed
- [x] 5.5 Verify all API endpoints return same format
- [x] 5.6 Verify TXT migration works: 15 accounts imported, TXT renamed to .migrated

**Validation:** PASSED - Full integration test with Flask test client
