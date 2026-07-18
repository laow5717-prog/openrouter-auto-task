# Concurrency Guidelines

> The daily pipeline can drive several browsers at once. These are the rules
> that keep that safe. Read this before touching `src/web/worker.py`,
> `run_daily_pipeline`, or anything that launches a browser.

---

## Model

Single process, worker threads. **The unit of parallelism is the account
(email)**, not the card — cards are resources an account consumes.

```
run_daily_pipeline (coordinator thread — never touches a browser)
  ├─ phase 0  build card pool          serial, DB only
  ├─ phase 1a top-up existing accounts pool.map(candidates)
  ├─ phase 1b register new accounts    pool.run_until_empty(claim_batch, ...)
  └─ phase 2  recharge                 per-round pool.map(), barrier at round end
```

Concurrency comes from `config.yaml`:

```yaml
concurrency:
  max_workers: 2              # 1-4, clamped
  claim_timeout_minutes: 20
```

`max_workers: 1` is the emergency rollback — it takes a **same-thread branch**
in `WorkerPool._dispatch`, so serial behaviour is structurally guaranteed rather
than an accident of pool sizing.

---

## The three exclusions

Every shared resource needs exactly one owner at a time. There are three, and
missing any one of them produces a different failure.

### 1. Account (email) — `AccountRegistry`

**Why**: `driver.py:_clear_singleton_locks` deletes Chrome's `SingletonLock`
unconditionally. Its comment says it is safe *because the app guarantees one
instance per profile*. Two workers on the same email delete each other's locks
and the browsers crash at random.

Profiles are keyed by email (`data/profiles/<email>`), so **email exclusion is a
hard constraint, not an optimisation**.

Must also be mutually exclusive with user-opened browsers: `claim()` and
`try_open_manual()` share one lock, so there is no window between "check if a
worker holds it" and "register in `open_browsers`".

### 2. Bound cards — DB `processing` state

See [database-guidelines.md](./database-guidelines.md). Needs to be persistent
(survives restart) and reapable, hence DB rather than memory.

### 3. Payment cards — `PaymentCardRegistry`

**Why, and this one is easy to miss**: the eligibility gate in
`registration.py` (`_eligible`: one-card-one-account, ≤2 charges per 24h, 3DS
cooldown) derives everything from the DB, and `_eligible_cards` is a **snapshot
taken on entry**. Concurrently, two workers both judge the same card eligible
and charge it for different accounts; by the time the DB rows are written the
rule is already broken.

The registry covers the window between "judged eligible" and "result written".
It layers on top of the DB rules, it does not replace them.

Cards are held **for the whole account run**, not per charge — releasing between
charges reopens the window for another worker to take the card mid-account.

---

## Hard constraints

**Playwright sync API is thread-bound.** A `BrowserSession` must be created and
used to completion on one thread. Never hand a driver to another thread; a
cross-thread `quit()` leaves the owning thread's pending call hanging forever.
This is why `force_stop()` is cooperative: it sets a flag, and each worker exits
at its own checkpoint and closes its own browser.

**`contextvars` do not propagate to new threads.** A new thread starts with an
empty context. `WorkerPool._run_in_worker` binds at the thread entry; anything
that spawns a sub-thread and logs must bind again. The serial branch runs on the
coordinator thread, so it restores the previous context with a token rather than
setting `None` — otherwise the binding leaks into later phases.

**Browsers are headed.** `headless=False` is hardcoded for anti-detection.
~300-500MB per instance is why `max_workers` caps at 4.

---

## Writing new parallel phases

- `pool.map(items, fn)` — barrier; use when the phase must finish before the
  next starts, or when per-round semantics matter (phase 2's anti-ban "one
  invoice per account per round" depends on this).
- `pool.run_until_empty(produce, fn)` — unbounded; `produce` must be thread-safe
  and is usually just a `claim_batch`. Atomic claiming *is* the work assignment.
- `fn(worker, item)` runs bound to `worker`. Use `worker.make_monitor(state)`
  for the monitor callback so screenshots and the active driver land on that
  worker.
- Always `release` what you claim in a `finally` — account, cards, both.
- Shared counters need a lock, and if you log the value, use the one the
  increment returned. Reading the counter afterwards gets whatever another
  worker has pushed it to.

## Never do

- `WHERE status='pending'` to decide whether work remains (misses `processing`)
- `SELECT` then `UPDATE` to take work (two workers get the same rows)
- Set `current_action` on `AppState` directly from worker code — use
  `set_action(worker, text)`, which keeps the global field meaningful in serial
  mode and lets the pipeline write an aggregate in parallel mode
- Expose `AppState.workers` directly to the API — use `active_workers()`, which
  truncates to the current concurrency. The dict only grows (logs are kept for
  review); showing all of it makes the `max_workers → 1` rollback look parallel.

## Testing

Everything above is covered by `tests/` with no browser involved:
`test_card_claim`, `test_registry`, `test_worker_pool`, `test_worker_state`,
`test_pipeline_concurrency`, `test_daily_pipeline`, `test_reaper`.

`test_daily_pipeline` replaces `create_driver` with a stub that raises. Keep
that: an un-stubbed call once launched a real Chrome window during a test run.
