# Database Guidelines

> SQLite database with raw SQL via a thin wrapper. No ORM.

---

## Overview

- Database: SQLite at `data/cloudflare_auto.db`
- Wrapper: `src/db/database.py` provides `execute()`, `fetchone()`, `fetchall()`
- All rows returned as `sqlite3.Row` (dict-like); convert with `dict(r)`
- Models: `src/models/account.py` (`AccountModel`), `src/models/card_binding.py` (`CardBindingModel`)

## Key Tables

### `accounts`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| email | TEXT | Unique |
| cf_password | TEXT | Cloudflare password |
| email_password | TEXT | mail.tm password |
| status | TEXT | registered, bound, failed, error |
| created_at | TEXT | datetime string |

### `card_bindings`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| task_id | INTEGER | Groups cards by task batch |
| card_display | TEXT | Last 4 digits |
| card_data_json | TEXT | Full card info as JSON |
| status | TEXT | pending, success, failed |
| bound_to_email | TEXT | Which account it's bound to |
| error | TEXT | Error message if failed |
| attempted_at | TEXT | datetime string |

## Query Patterns

### Pagination
All paginated queries follow:
```python
def get_paginated(self, page=1, page_size=20, **filters):
    conditions = []
    params = []
    # build WHERE from filters
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size
    total = self.db.fetchone(f"SELECT COUNT(*) as cnt FROM table{where}", params)['cnt']
    rows = self.db.fetchall(f"SELECT ... FROM table{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            params + [page_size, offset])
    return [dict(r) for r in rows], total
```

### Date Range Filter
```python
if date_from:
    conditions.append("column >= ?")
    params.append(date_from)
if date_to:
    conditions.append("column <= ?")
    params.append(f"{date_to} 23:59:59")
```

### Keyword Search (multi-column)
```python
conditions.append("(col1 LIKE ? OR col2 LIKE ?)")
params.extend([f"%{keyword}%", f"%{keyword}%"])
```

## Common Mistakes

- Always use parameterized queries (`?` placeholders) — never f-string user input into SQL
- `fetchone()` can return `None` — always check before accessing fields
- `card_data_json` must be parsed with `json.loads()` wrapped in try/except
