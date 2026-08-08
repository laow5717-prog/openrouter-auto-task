# Database Guidelines

> SQLite database with raw SQL via a thin wrapper. No ORM.

---

## Overview

- Database: SQLite at `data/openrouter_auto.db`（frozen 模式 `~/.openrouter-auto-task/`）
- Wrapper: `src/db/database.py` provides `execute()`, `fetchone()`, `fetchall()`
- All rows returned as `sqlite3.Row` (dict-like); convert with `dict(r)`
- Models: `src/models/account.py` (`AccountModel`), `src/models/card_binding.py` (`CardBindingModel`)

## Key Tables

### Identity vs platform: `accounts` + `platform_accounts`

Account data is split across two tables. Getting this split wrong is the single
most common source of cross-platform bugs, so read this before touching either.

**`accounts` holds identity only** — things that are true of the person/mailbox
regardless of which site we are automating:

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| email | TEXT | Unique |
| login_password | TEXT | **The GitHub password.** Not a platform password — opencode logs in via GitHub OAuth |
| email_password | TEXT | Mailbox password |
| email_verify_link | TEXT | ruoanzhu inbox link, used to auto-collect device-verification codes |
| identity_status | TEXT | GitHub signup/ban outcome: `imported` / `registered` / `pending` / `failed` / `suspended` / `rejected` / `flagged` |
| status | TEXT | **Dead column.** Frozen at its pre-split value; kept only so a code rollback still reads something sensible. Never write it |
| created_at, updated_at | TEXT | datetime strings |

**`platform_accounts` holds one row per (platform, email)** — everything that is
specific to one target site:

| Column | Type | Notes |
|--------|------|-------|
| platform | TEXT | Slug, e.g. `opencode`. `UNIQUE(platform, email)` |
| login_password | TEXT | That platform's own password. Empty for OAuth platforms |
| status | TEXT | Business terminal states: `archived` / `recharged` / `subscribed` |
| tenant_id | TEXT | Platform-side workspace id (opencode's `wrk_xxx`) |
| credits_balance, balance_updated_at | | Balance last read on that platform |
| apikey, apikey_updated_at | | API key scraped from that platform |

**"No row" is a meaningful state**: it means the mailbox has not been onboarded
to that platform yet. Do not insert an empty-status row to represent it — that
conflates "not onboarded" with "onboarded, status unknown", and the AdsPower
reclaim query depends on telling those apart.

Why two tables and not three (mailbox / GitHub / platform): mailbox and GitHub
account are strictly 1:1 today — each hotmail registers exactly one GitHub
account. Splitting that 1:1 into its own table would be hollow normalization.
If "one mailbox, several GitHub accounts" ever becomes real, split `accounts`
then; `platform_accounts` is unaffected.

**Terminal-status constants live in `src/utils.py`**, not inline:
`IDENTITY_TERMINAL_STATUSES` (banned/suspended/rejected/flagged — dead for every
platform) and `PLATFORM_TERMINAL_STATUSES` (archived/recharged/subscribed — done
on *this* platform). They were previously hardcoded in two places that had
drifted apart, which is why the start gate and the pipeline used to disagree on
how many accounts were eligible.

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

#### Card state is split by platform-dependence

A card's state lives in two places, split by one question: *is this true of the
card itself, or only of what happened on one site?*

| Where | Holds | Why |
|---|---|---|
| `card_pool.status` | `'expired'` or `''` | Expiry is a property of the card. Same on every platform |
| `card_platform_state(card_number, platform)` | `'bound'` / `'invalid'` / `'paid'` | All three describe *what happened on one site* — bound to an account there, declined by that merchant, paid successfully there. Change the platform (different Stripe merchant, different risk rules) and none of them carry over |

The effective status for a platform is expressed once, in
`card_pool._EFF_STATUS`, and reused by every query in that module:

```sql
COALESCE(NULLIF(cp.status,''), NULLIF(cps.status,''), '')
```

The UI buckets are still **derived**, now per-platform:

| Bucket | Definition |
|---|---|
| `invalid` | effective status `IN ('expired','invalid')` (= `CARD_STATUS_UNUSABLE`) |
| `valid` | not invalid **AND** `card_number IN (SELECT … FROM valid_cards WHERE platform=?)` |
| `unverified` | not invalid **AND** not in that platform's `valid_cards` |

The `WHERE platform=?` on the `valid_cards` subquery is load-bearing, not
decoration. `valid_cards` membership used to be treated as a global invariant,
and `mark_invalid_by_number` refused to invalidate any card in it. On a second
platform that means a card that succeeded once on opencode can *never* be marked
invalid — it gets re-picked and re-declined every round until the quota is gone.

Always go through `CardPoolModel._bucket_where(bucket, platform)` so all call
sites share one definition. Anything that *counts* or *moves* by bucket must call
`refresh_expired_status(group_id)` first (see `count_buckets`,
`delete_invalid_by_group`, `move_bucket_to_group`) — a card whose expiry has
passed but has not been re-stamped yet would otherwise be counted as
`unverified`.

Do not confuse this with `card_bindings.status`, whose `'pending'` is a real
stored value for the *binding task*, unrelated to the card pool.

#### `card_payment_state` carries two independent things

Same primary key `(card_number, platform)`, two unrelated meanings. Both are
written by `registration.recharge_account` and read by `_eligible_cards`:

| Columns | Meaning | Written when |
|---|---|---|
| `tds_until`, `tds_reason` | **Temporary cooldown** — "don't pick this card again until then" | Any declined payment, or a 3DS challenge |
| `fail_streak`, `last_fail_at` | **Consecutive-failure count** — how many times in a row this card was declined *on this platform* | +1 on every decline, reset to 0 on any success |

The two must not clobber each other. `set_cooldown`'s `ON CONFLICT DO UPDATE`
lists only the `tds_*` columns; `reset_fail_streak` only `UPDATE`s (never
inserts), so a card that has never failed does not get a row of zeroes.

**A card is invalidated only when `fail_streak` reaches
`cfg.recharge.fail_threshold()` (default 3).** Read the threshold through that
method, never `max_fail_streak` directly: the comparison is `streak >=
threshold` and `streak` counts from 1, so a hand-edited `0` in `config.yaml`
would make the condition vacuously true and write off every card on its first
decline. `fail_threshold()` floors it at 1. Before that, a decline just cools
the card down. Combined with `fail_cooldown_hours` (default 24), writing off a
genuinely dead card takes three days. That slowness is the point — the previous
rule invalidated on the *first* decline, so one issuer hiccup permanently burned
a good card.

Two consequences that surprise people reading the pipeline logs:

- The "分组可选卡耗尽" stop condition now fires with most cards merely *cooling
  down*, not invalid. Card counts in the UI will not drop the way they used to.
- A successful payment does **not** cool the card down. It must not — one
  account now tops up repeatedly in a single session **on the same card** (see
  `recharge_account`'s sticky loop), and cooling successful cards would leave it
  with nothing to charge on the second round.

#### Card ordering: proven cards first

`AppState._eligible_cards` orders candidates `good + fresh` — cards that have a
`recharge_logs` success on this platform come first, never-charged cards last.
This is the reverse of the original ordering, which fed new cards first to "work
the pool down". In practice that gambled every single charge on an unverified
card: high decline rate, velocity risk stacked on the account, while the few
cards that actually clear sat at the tail of the queue. Fresh cards are now only
reached once the proven ones are all cooling down or written off.

The same ordering feeds the subscribe pipeline (`_subscribe_one_account`), which
shares this method.

`bump_fail_streak` does its upsert and read-back inside `Database.transaction()`.
Splitting them into two `execute()` calls loses counts under concurrency: two
workers declining the same card would each read the same old value, the counter
would only advance by one, and a bad card could never reach the threshold.

The three outcomes in `OUTCOMES_KEEPING_CARD` (`error`, `needs_captcha`,
`unknown`) touch **neither** column. They are not the card's fault, and a
network blip must never push a good card toward being written off.

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

## Migrations

Versioned by `PRAGMA user_version`; `_MIGRATIONS[n]` runs once, in order.
Never edit a historical migration — add a new one. (V7 still creates
`invoice_payment_state` even though V13 drops it; that is correct.)

`_migrate` executes **statement by statement**, not via `executescript`, and
skips `ALTER TABLE … ADD COLUMN` when the column already exists. Without that,
one already-applied `ADD COLUMN` fails the whole script — which happens every
time you rehearse a migration on a copy of the production database, i.e. exactly
when you most need it to work. Statement boundaries come from
`sqlite3.complete_statement`, not from splitting on `;` (that would break on a
semicolon inside a string literal).

SQLite cannot alter a primary key or a UNIQUE constraint. Adding `platform` to
`valid_cards` / `card_payment_state` required create-new → copy → drop → rename.

When adding a column that call sites must supply, default it to `''` and
`UPDATE` existing rows to the real value — **not** `DEFAULT 'opencode'`. A
default that looks plausible turns "caller forgot the parameter" into rows that
silently belong to the wrong platform, which is far harder to find than a blank.

### Backing up before a migration

**`cp` is not a backup.** The database runs in WAL mode; a bare copy of the
`.db` file gives you a stale snapshot (measured once: 39 accounts / 1737 recharge
logs, versus the real 36 / 1935). Use the SQLite backup API:

```python
src = sqlite3.connect('data/openrouter_auto.db')
dst = sqlite3.connect('data/openrouter_auto.db.bak-<date>-<label>')
src.backup(dst)
```

Rehearse every migration on such a copy and compare row counts per table before
touching the real file.

## Common Mistakes

- Always use parameterized queries (`?` placeholders) — never f-string user input into SQL
- `fetchone()` can return `None` — always check before accessing fields
- `card_data_json` must be parsed with `json.loads()` wrapped in try/except
- **Never write `SELECT t.*, <expr> AS status` when `t` already has a `status`
  column.** `sqlite3.Row` keeps only the first of two same-named columns, so the
  computed one is silently dropped — no error, and the data really is in the
  table, so there is nothing to grep for. List the columns explicitly
  (`card_pool._CP_COLS` exists for this reason).
- **Placeholder order follows the SQL text, not the logical structure.** In most
  joined queries the JOIN's `?` comes first, but if the SELECT list itself
  contains placeholders (e.g. `count_buckets`' three `SUM(CASE …)` expressions),
  those bind *before* the JOIN's. Getting it wrong does not raise — it silently
  returns zeros.
