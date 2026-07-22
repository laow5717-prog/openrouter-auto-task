# Database Guidelines

> SQLite database with raw SQL via a thin wrapper. No ORM.

---

## Overview

- Database: SQLite at `data/openrouter_auto.db`（frozen 模式 `~/.openrouter-auto-task/`）
- Wrapper: `src/db/database.py` provides `execute()`, `fetchone()`, `fetchall()`
- All rows returned as `sqlite3.Row` (dict-like); convert with `dict(r)`
- Models: `src/models/account.py` (`AccountModel`), `src/models/card_binding.py` (`CardBindingModel`)

## Key Tables

### `accounts`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| email | TEXT | Unique |
| login_password | TEXT | 站点登录密码（原 `cf_password`，改造 OpenRouter 时更名） |
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
| status | TEXT | pending, **processing**, success, failed |
| bound_to_email | TEXT | Which account it's bound to |
| error | TEXT | Error message if failed |
| attempted_at | TEXT | datetime string |
| worker_id | TEXT | Which worker claimed it (parallel execution) |
| claimed_at | TEXT | When it was claimed; used for stale-claim reaping |

#### `card_bindings` status machine

```
pending --claim_batch--> processing --> success | failed
   ^                          |
   +--release_unused / reap_stale
```

**Counting rule**: `pending` in `get_summary()` / `get_global_summary()` means
*not finished* — it counts `pending + processing`. A separate `processing` field
carries the detail. Anything asking "are there cards left to do?" must use this
combined figure, never `WHERE status='pending'` alone: cards held by a live
worker are still outstanding work.

**Claiming must be atomic.** Never `SELECT` then `UPDATE` — two workers will
select the same rows. Use `CardBindingModel.claim_batch()`, which does the
select-and-mark in one statement. This is safe because `Database` shares a
single connection behind one `threading.Lock`, so each `execute()` is
serialized. **If that ever changes** (connection pool, multi-process), rewrite
`claim_batch` with an explicit `BEGIN IMMEDIATE` transaction.

#### `card_pool` status buckets are derived, not stored

`card_pool.status` only ever holds `''` / `'paid'` / `'expired'` / `'invalid'` /
`'failed'`. The buckets the UI shows are **derived**:

| Bucket | Definition |
|---|---|
| `invalid` | `status IN ('expired','invalid')` (= `CARD_STATUS_UNUSABLE`) |
| `valid` | not invalid **AND** `card_number IN valid_cards` |
| `unverified` | not invalid **AND** `card_number NOT IN valid_cards` |

There is no `status='unverified'` row anywhere — writing `WHERE status='pending'`
or similar silently returns nothing. Always go through
`CardPoolModel._bucket_where(bucket)` so all call sites share one definition.

Anything that *counts* or *moves* by bucket must call
`refresh_expired_status(group_id)` first (see `count_buckets`,
`delete_invalid_by_group`, `move_bucket_to_group`) — a card whose expiry has
passed but has not been re-stamped yet would otherwise be counted as
`unverified`.

Do not confuse this with `card_bindings.status`, whose `'pending'` is a real
stored value for the *binding task*, unrelated to the card pool.

### Multi-statement transactions

`Database.execute()` commits every statement. When a batch must be atomic, use
`Database.transaction()`:

```python
with self.db.transaction() as conn:
    for row in rows:
        conn.execute("UPDATE ... WHERE id=?", (row['id'],))
```

**Inside the block, use the yielded `conn` only.** Calling
`self.db.execute/fetchone/fetchall` there deadlocks — `_lock` is a plain
`threading.Lock`, not reentrant — and would commit mid-transaction anyway. Do
reads before opening the block.

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
