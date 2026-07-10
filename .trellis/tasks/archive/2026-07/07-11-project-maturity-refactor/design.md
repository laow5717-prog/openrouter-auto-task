# Design: Project Maturity Refactor

## Architecture Overview

```
server.py (entry point)
  -> src/web/app.py       create_app() factory
       -> src/api/routes.py   Flask Blueprint, all /api/* endpoints
       -> src/models/         SQLite data layer
       -> src/services/       Business logic (registration, email, card, captcha)
       -> src/browser/        Selenium automation (unchanged logic)
       -> src/config.py       YAML config loader (cleaned up)
```

## Module Boundaries & Import Graph

```
src/config.py        (leaf - no src imports)
src/utils.py         (leaf - imports config)
src/models/*         (imports config, utils)
src/browser/*        (imports config, captcha_solver)
src/services/*       (imports models, browser, config, utils)
src/api/*            (imports services, models, config)
src/web/app.py       (imports api, config; creates Flask app)
```

No circular dependencies. Each layer only imports from layers below it.

## Data Layer Design (SQLite)

### Connection Management

`src/models/database.py` provides a singleton `Database` class:
- `Database(db_path)` - opens/creates SQLite file, runs migrations
- Thread safety via `check_same_thread=False` + module-level lock for writes
- `db.execute(sql, params)` / `db.fetchone()` / `db.fetchall()` convenience wrappers
- Auto-creates `data/` directory if missing
- Schema version tracking via `PRAGMA user_version`

### Schema

```sql
-- v1 initial schema
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    cf_password TEXT,
    email_password TEXT,
    status TEXT DEFAULT 'registered',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,          -- 'batch' | 'card_driven'
    status TEXT DEFAULT 'running', -- 'running' | 'completed' | 'stopped'
    total_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    config_json TEXT,            -- snapshot of task parameters
    started_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS card_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    card_display TEXT NOT NULL,  -- last 4 digits only
    status TEXT DEFAULT 'pending', -- 'pending' | 'success' | 'failed'
    bound_to_email TEXT,
    error TEXT,
    attempted_at TEXT,
    card_data_json TEXT          -- encrypted/full card data for runtime use only
);
```

### Migration Strategy

- `PRAGMA user_version` tracks schema version (0 = fresh, 1 = v1 schema)
- `database.py` checks version on init, applies migrations sequentially
- TXT import: on first run, if `registered_accounts.txt` exists and `accounts` table is empty, parse and import all rows, then rename TXT to `.txt.migrated`

## Service Layer Changes

### Registration Service (`src/services/registration.py`)
- Extracted from `main.py`
- Functions accept `db: Database` parameter instead of using global state
- `register_one_account()` writes to DB via `AccountModel.upsert()` instead of `save_to_txt()`
- `run_card_driven_batch()` uses `CardBindingModel` instead of in-memory `CardTracker`

### AppState Refactor
- `AppState` stays in-memory for real-time data (logs, current frame, is_running)
- Task results persisted to DB via `TaskModel`
- On server restart, completed tasks are queryable from DB
- Logs remain in-memory only (ephemeral by nature)

### Card Tracker
- `CardTracker` becomes a thin wrapper around `CardBindingModel`
- `mark_success()`/`mark_failed()` write directly to DB
- `get_pending_cards()` queries DB instead of filtering in-memory list
- Survives server restart mid-task

## API Layer

`src/api/routes.py` as Flask Blueprint:
- All endpoint paths unchanged (`/api/status`, `/api/start`, etc.)
- `/api/accounts` reads from DB instead of parsing TXT file
- `/api/card/status` reads from DB
- Response format unchanged - frontend needs zero changes

## Config Changes

- Remove all module-level constant exports (`TOTAL_ACCOUNTS`, `EMAIL_WORKER_URL`, etc.)
- Services import `cfg` directly from `src.config`
- `config.yaml` format unchanged
- Add `database.path` config option (default: `data/cloudflare_auto.db`)

## PyInstaller Compatibility

- `build.py` updated: entry point remains `server.py`, add `--collect-submodules src`
- `--add-data` for `static` stays the same
- `sys.frozen` / `sys._MEIPASS` handling moved to `src/config.py`
- `data/` directory auto-created at runtime next to executable

## Rollback

Since this is a restructure, rollback = revert to the commit before the refactor. The TXT file is preserved (renamed to `.migrated`), so no data loss.
