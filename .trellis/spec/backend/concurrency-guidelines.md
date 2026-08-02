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

**Why**: `driver.py:_clear_singleton_locks` deletes Chrome's `SingletonLock`.
Two workers on the same email delete each other's locks and the browsers crash
at random.

That function used to justify the deletion with a comment saying it was safe
*because the app guarantees one instance per profile*. It now **verifies** the
premise instead of assuming it: `_kill_chrome_for_profile` reclaims any process
still holding the `user-data-dir` before the locks come off. That is a
belt-and-braces measure for orphans left by crashed browsers — it does **not**
relax the email exclusion below. A live worker's Chrome is indistinguishable
from an orphan at the process level, so two workers on one email would still
kill each other's browsers.

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

## Recharge pipeline: worker-autonomous register+recharge

`run_daily_pipeline` is a single `pool.run_until_empty(_produce, _do)` at
`cfg.concurrency.max_workers` (not a hardcoded serial map). Each worker
autonomously pulls one account and either recharges it or, if none are payable,
registers one `imported` account — so registration and recharge run concurrently
across workers, not in separate phases.

**`_produce` (guarded by `produce_lock`)** finds-and-claims atomically: it scans
`_payable_now()` first, else `_registerable_imported()`, and returns the first
account it can `account_registry.claim()`. The lock spanning find+claim is what
stops two workers taking the same account (mirrors `_register_bind_loop._produce`).

**`_do` (per worker)** releases the account in `finally` and updates a `done` set
+ `stats` under `state_lock`. Three de-dup / termination invariants live here:

- **Success de-dup**: recharge success sets `status='recharged'`, excluded by
  `_payable_now` (alongside banned/archived/flagged and any email in `done`).
- **Register→recharge closure**: a freshly-registered account is **not** added to
  `done`; after release, the next `_produce` sees it as `registered`+password and
  hands it to whichever worker is free — the login→recharge step, possibly on a
  different worker than registered it.
- **Termination**: recharge *failure* adds the email to `done` (was the old
  `done_emails`+`progressed` backstop) so a card-declining account is never
  re-pulled forever; archived/flagged/failed-registration also land in `done`.
  Every account thus reaches recharged or `done`, and `imported`/payable are
  finite, so `_produce` eventually returns None and all workers exit. There is no
  round counter and no `MAX_ROUNDS` — `run_until_empty` converges by draining.

Because both stubbed methods (`_recharge_one_account`, `_register_one_account`)
only touch the passed `worker` and never write `self` counters unlocked, the only
shared mutable state is `done`/`stats`, both under `state_lock`. Card exclusion is
still `PaymentCardRegistry` inside `recharge_account` (per-card). See
`tests/test_daily_pipeline.py` for the concurrency assertions (no overlap, peak>1,
clean release, serial==parallel ledger).

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
