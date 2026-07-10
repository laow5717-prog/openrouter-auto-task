# PRD: Project Maturity Refactor

## Background

cloudflare-auto-task is a Cloudflare account auto-registration & credit card binding tool with a Flask web UI. Currently all source files are flat in the root directory, data is stored in a plain text file (`registered_accounts.txt`), and runtime state lives only in memory. The project needs to be restructured as a mature, extensible application with robust local data storage.

## Goals

1. **Local SQLite database** - Replace TXT file storage with SQLite for accounts, task history, and card binding records. All data stays on the local machine.
2. **Package structure** - Reorganize flat .py files into a proper Python package layout with clear module boundaries.
3. **Extensible architecture** - Clean separation of concerns (data layer, business logic, API, frontend) to make future feature additions straightforward.

## Current Pain Points

- Account data stored in `----`-delimited TXT file - fragile, no querying, no indexing
- `CardTracker` state is in-memory only - lost on restart
- `AppState` (logs, running status) is in-memory only
- All 9 .py files flat in root - no package structure
- `server.py` mixes routing, state management, and business logic
- Config uses global module-level constants exported for backward compatibility

## Requirements

### R1: SQLite Local Database
- Use SQLite via Python `sqlite3` (no ORM dependency bloat)
- Database file stored at `data/cloudflare_auto.db` (relative to project root)
- Tables:
  - `accounts` - email, cf_password, email_password, status, created_at, updated_at
  - `tasks` - task_id, type, status, started_at, finished_at, config_snapshot
  - `card_bindings` - card_display (last 4 digits only, never store full card number), status, bound_to_email, error, attempted_at, task_id
- Migrate existing `registered_accounts.txt` data on first run
- All current TXT read/write paths replaced with DB calls

### R2: Project Package Structure
```
cloudflare-auto-task/
  src/
    __init__.py
    models/          # SQLite data access
      __init__.py
      database.py    # DB connection, schema, migrations
      account.py
      task.py
      card_binding.py
    services/        # Business logic
      __init__.py
      registration.py   # (from main.py)
      email.py          # (from email_service.py)
      captcha.py        # (from captcha_solver.py)
      card.py           # (from card_manager.py)
    browser/         # Browser automation
      __init__.py
      driver.py         # (from browser.py)
    api/             # Flask routes
      __init__.py
      routes.py         # (from server.py API handlers)
    web/             # Web server setup
      __init__.py
      app.py            # Flask app factory, static serving
    config.py        # (from config.py, cleaned up)
    utils.py         # (from utils.py, trimmed)
  static/            # Frontend assets (unchanged)
  data/              # SQLite DB, uploads, reports
  config.yaml
  server.py          # Thin entry point
  pyproject.toml
```

### R3: Extensible Architecture
- Flask app factory pattern (`create_app()`)
- Database singleton with thread-safe connection handling
- Services accept DB handle, no global state
- `AppState` persists task/log state to DB so it survives restarts
- Card tracker backed by DB instead of in-memory list
- Clean import graph: models -> services -> api (no circular deps)

### R4: Migration & Compatibility
- Existing `config.yaml` format unchanged
- Web UI API endpoints unchanged (frontend works without changes)
- `registered_accounts.txt` auto-imported into SQLite on first run
- PyInstaller build continues to work

## Non-Goals

- No external database server (MySQL, PostgreSQL, etc.)
- No ORM (SQLAlchemy, etc.) - keep it simple with raw sqlite3
- No authentication/login system for the web UI
- No frontend framework migration (keep vanilla JS)
- No new features - this is a refactor only

## Acceptance Criteria

- [ ] `python server.py` starts the web server and all existing functionality works
- [ ] Account data persists in `data/cloudflare_auto.db` instead of TXT
- [ ] Card binding records persist across restarts
- [ ] Task history is queryable from the accounts page
- [ ] All existing API endpoints return the same response format
- [ ] `registered_accounts.txt` is auto-migrated on first run if present
- [ ] No new external dependencies (sqlite3 is stdlib)
- [ ] Project passes basic import sanity check: `python -c "from src.web.app import create_app"`
