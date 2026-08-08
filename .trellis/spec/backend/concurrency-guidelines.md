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

### 3b. Proxy IPs — `ProxyRegistry`

Same shape as `PaymentCardRegistry` (worker.py) — an in-memory `proxy_key →
worker_id` map under a lock. Each account run acquires one free proxy so two
concurrent workers never share an exit IP (which would re-link the accounts the
proxy was meant to isolate). `key_of(proxy) = "host:port:username"`.

Allocation is in `run_daily_pipeline._produce` (under `produce_lock`, alongside
the account claim): `acquire_free(usable, worker_id)` returns the first
un-held proxy; if all are held — only possible when workers > proxies, since 100
proxies ≫ 2 workers — it falls back to `usable[account_id % N]` **without**
exclusivity (the "循环复用" the user asked for). The exclusive-held proxy is
released in `_do`'s finally (the modulo-fallback one is not, signalled by a null
`proxy_key`). No proxies configured → `proxy=None` → direct connection, same as
before. Released in the task finally via `proxy_registry.release_all()`.

Trade-off baked in by the user's choice of dynamic allocation: **a given account
may use a different exit IP across runs**. The proxies table keeps an
`assigned_email` column so a future "one account, one fixed IP" mode needs no
schema change.

`ProxyRegistry` applies to the **local browser stack only**. When
`adspower.enabled` is on, `_acquire_proxy_for` returns `(None, None)` and the
proxy comes bound to the AdsPower profile instead — handing out a proxy on both
sides would put the browser behind two of them. Exclusivity there is enforced by
the server's `profile_count`, not by this registry; see
[adspower-guidelines.md](./adspower-guidelines.md).

### 3. Payment cards — `PaymentCardRegistry`

**Why, and this one is easy to miss**: the eligibility gate in
`registration.py` (`_eligible`: one-card-one-account, ≤2 charges per 24h, 3DS
cooldown) derives everything from the DB, and `_eligible_cards` is a **snapshot
taken on entry**. Concurrently, two workers both judge the same card eligible
and charge it for different accounts; by the time the DB rows are written the
rule is already broken.

The registry covers the window between "judged eligible" and "result written".
It layers on top of the DB rules, it does not replace them.

Cards are released **per card**, not per account run. Holding them for a whole
account run starved other workers on a tight pool, and `registration` reads "no
cards available" as *pool exhausted*, so the orchestrator permanently abandoned
those accounts — temporary contention misread as permanent exhaustion. Guard:
`test_release_lets_a_waiting_worker_proceed`.

"Per card" is not "per charge": `recharge_account` **sticks to a card that
works** and charges it repeatedly until it fails, holding the acquisition for
that whole run of charges. The bound on how long is `max_card_attempts` and
`balance_cap`, both re-checked on every charge inside the inner loop — with the
sticky loop, "we ran out of cards" is no longer a natural brake, so those two are
the only ones left. Other workers are not blocked by a held card: `try_acquire`
failing makes them `continue` to the next candidate without counting an attempt.

### Round-scoped card ownership — a second, softer gate

Per-charge release alone lets worker B pick up a card the moment worker A is done
with it. `_eligible_cards` is a **snapshot taken on entry**, and parallel workers
walk the same ordered list seconds apart, so B's snapshot predates A's
invalidations. Measured 2026-08-03: five cards were charged twice in one round,
once per account, and the second attempt was guaranteed to decline — the card had
already been declined and marked `invalid`. Pure waste, plus velocity risk on the
account.

`PaymentCardRegistry._used` records the first account to touch each card and
survives `release()`, clearing only on `release_all()` at end of round.
**It is not enforced in `try_acquire`** — that would recreate the starvation bug
above. Enforcement lives in `AppState._exclude_used_this_run`, called from
`_eligible_cards`, and it **falls back to the unfiltered list when every
candidate has been used**. Better to repeat one card occasionally than to abandon
an account.

### The two levels have deliberately opposite platform scope

| Level | Key | Scope |
|---|---|---|
| `_in_flight` | `card_number` | **Global.** Not a concurrency question — submitting the same card at two merchants at once stacks issuer velocity risk. The issuer sees the card, not which platform we happen to be running |
| `_used` | `(platform, card_number)` | **Per platform.** Pure round-dedup heuristic; "tried on opencode" says nothing about another site, where the card is still untouched |

`release_all()` therefore has **three** modes：

| 调用 | 场景 | 效果 |
|---|---|---|
| `release_all()` | 进程退出 | 全清 |
| `release_all(平台)` | **轮边界** | 只清该平台本轮归属，`_in_flight` 不动 |
| `release_all(平台, include_in_flight=True)` | **该平台任务收尾** | 连它的 in-flight 一起放开 |

轮边界清 `_in_flight` 会在跑动中丢掉发卡行 velocity 防护。

⚠️ **多平台并发时，任务收尾绝不能用无参形式**——那会连另一个平台正在刷的卡一起
从 `_in_flight` 抹掉，于是同一张卡可能被两个平台同时提交给发卡行。

### owner：所有共享注册表都要能区分「谁占的」

三个注册表都跨平台共享，语义分两类，**不能一刀切**：

- **排他判定不带 owner**（`is_claimed` / `try_acquire` 的准入）——排他本来就是全局的
- **「本轮还有谁在飞」与「收尾释放」必须带 owner**（`snapshot(owner)` /
  `release_all(owner)`）

`ProxyRegistry` 的持有者身份是 `(worker_id, owner)` 而不是裸 `worker_id`：
每个平台各有一套同名的 `W1..W4`，只比 worker_id 的话两个平台的 W1 会互认成自己，
**同一出口 IP 被同时发给两边**，反关联白做且不留任何日志。

三种漏带 owner 的后果全都不报错，详见
`.trellis/spec/backend/multi-platform-guidelines.md` 的「owner」一节。

Counting call sites (`len(_eligible_cards(...))` for "how many cards remain" and
"is there work left") must pass `exclude_used=False`, or the number shrinks as the
round progresses and the pipeline decides the pool is empty and stops early.

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

## Budget gates: exclusion is not enough

A gate that asks "has this account had enough yet?" cannot be built from DB
state alone, even with `AccountRegistry` guaranteeing one worker per email.
The account-reuse cap (`_reusable_recharged`, `balance_cap`) learned this twice
on 2026-08-05; both failures were silent overspend, not crashes.

### 1. The window opens *after* the claim is released

`claim` only covers "while A is running". A writes its ledger row, `_do`'s
`finally` releases the claim, and B claims the same account legitimately — but
if B's view of the ledger predates A's write, B sees an account that is still
"under cap" and charges it again. Measured: ~4 runs in 10 at 3 workers,
`balance_cap` $60 charged 4×$20.

The fix is the `PaymentCardRegistry` pattern — an **in-memory ledger written at
claim time**, consulted alongside the DB one. Charge it on claim, refund it in
`finally` when nothing was actually spent.

### 2. Read the in-memory ledger *before* the DB one

Adding the in-memory ledger cut failures to ~1 in 10, not zero. Reading it
second reopens the hole:

```
read topped = 40  →  [A finishes, DB becomes 60, A's finally clears its entry]
                  →  read in-flight = 0
→ effective = 40  →  under cap  → both gates miss the same $20
```

The money hands off between the two ledgers: the in-memory entry disappears
*after* the DB row appears. Read the in-memory one first and the handoff is
always covered by at least one side. `_payable_now`'s "platform status must be
read last, it is the exclusion criterion" is the same constraint seen from the
other end.

### 3. Do not let convergence depend on the DB ledger being written

If the only thing pushing an account toward its cap is a `recharge_logs` row,
then a run where those rows are missing, zero-valued, or filtered out by
platform/timestamp never terminates — the account stays forever "under cap" and
is re-claimed indefinitely. This is not an assertion failure; the process spins
until killed. It reproduced immediately in the test suite, where stub trackers
did not write the log table.

The in-memory ledger must therefore be **monotonic per run** and combined as
`max(db_amount, in_memory_amount)` — not a sum, since both record the same
money and adding them retires accounts early. The DB figure is more accurate
(a session may charge several times), the in-memory figure is more reliable.

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

**`_produce` (guarded by `produce_lock`)** finds-and-claims atomically. Two tiers,
in order:

1. **Ready accounts** — `_payable_now() + _reusable_recharged()`, scanned as one
   list. New accounts and already-recharged-but-under-cap ones compete equally;
   the two lists never intersect because `recharged` is a platform-terminal
   status, which `_payable_now` excludes.
2. **Accounts still needing signup** — `_registerable_imported()`.

Registration is last on purpose. It is slow and the resulting account is the one
most likely to get flagged by GitHub; when dozens of `imported` rows sat ahead of
the reuse pool, a handful of $20 accounts starved indefinitely while every worker
ground through signups.

The lock spanning find+claim is what stops two workers taking the same account
(mirrors `_register_bind_loop._produce`).

**`_do` (per worker)** releases the account in `finally` and updates a `done` set
+ `stats` under `state_lock`. Three de-dup / termination invariants live here:

- **Success de-dup**: recharge success sets `status='recharged'`, excluded by
  `_payable_now` (alongside banned/archived/flagged and any email in `done`).
- **Register→recharge closure**: a freshly-registered account is **not** added to
  `done`; after release, the next `_produce` sees it as `registered`+password and
  hands it to whichever worker is free — the login→recharge step, possibly on a
  different worker than registered it.
- **Termination**: recharge *failure* only adds the email to `failed_this_round`,
  so the account is retried on the next round (cards are the scarce resource;
  burning through the pool is the goal). Permanent exits — archived, flagged,
  failed registration — land in `done`. Convergence therefore rests on the
  round boundary, not on accounts draining.

**The round boundary decides whether the run ever ends**, and the product
requirement is blunt: *run until the card pool is exhausted*. Progress means
*either* a card was paid *or* the selectable-card set **changed at all** —
gaining members (cooldown expired, cards imported) **or losing them** (a card was
declined and went to cooldown / invalid). Burning cards is progress.

Termination does not come from a round cap. It comes from the card set being
finite and monotonically consumed: every failed attempt removes a card, so
`分组可选卡已耗尽` is always reachable and is the **primary** convergence path for
a fully-failing run.

The one early stop that remains is a **fully idle round**: nothing paid *and* not
a single card moved. That means the flow never reached the card-trying step at all
(login broke, profile would not start, hCaptcha timed out), so another round can
only repeat itself and the card-exhaustion path is unreachable.
`IDLE_ROUNDS_LIMIT = 2` tolerates one round of transient noise.

> **Both directions of this have burned us — read before "fixing" it again.**
> Originally the sets were compared with `!=`, so shrinkage counted as progress
> and the counter never advanced: on 2026-08-05 a run went **113 rounds**, two
> accounts failing in a loop, pool ground from 3426 to 2796, no error logged,
> stopped by hand. The fix — count only *gained* cards — then failed the opposite
> way on 2026-08-06: 0 payments succeeded, so two rounds after start the run
> converged with **2596 selectable cards never tried**. The current rule (any
> change counts, idle-only stop) is the deliberate third position: the 113-round
> scenario is now *expected behaviour*, bounded by the pool rather than by rounds.
> Guards: `test_burning_cards_keeps_running_until_pool_is_exhausted`,
> `test_idle_two_rounds_then_stop`,
> `test_idle_counter_resets_after_a_round_that_burned_cards`.

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
