# AdsPower Fingerprint Browser

> How the daily pipeline drives browsers when `adspower.enabled` is on. Read this
> before touching `src/services/adspower.py`, `src/browser/adspower_driver.py`, or
> the `browser_factory` wiring in `src/web/app.py`.

Two browser stacks coexist. `cfg.adspower.enabled` picks between them, and every
integration point takes the same shape: **`browser_factory is None` → old local
path, unchanged**. That is the only rollback mechanism and it needs no code edit.

| Mode | Session created by | Proxy comes from |
|------|--------------------|------------------|
| `enabled: false` (default) | `create_driver_vanilla` — local Chrome, `data/profiles/<email>` | DB `proxies` table via `ProxyRegistry` |
| `enabled: true` | `create_driver_adspower` — CDP takeover of an AdsPower profile | AdsPower proxy list, bound at profile creation via `proxyid` |

---

## Facts established by measurement, not by reading the docs

All verified on this machine on 2026-08-03. Re-verify before assuming any of them
changed — several contradict the published documentation.

**The environment quota is 12, and it is the binding constraint.** Creating the
13th profile returns `code:-1` with `If the number of imported accounts exceeds
the limit of 12, please delete some accounts and try again.` Accounts number in
the hundreds, so profiles are a *scarce, recycled* resource — reclaim is the main
path, not a fallback. There are 100 proxies, so proxies are never the bottleneck.

**Auth is `Authorization: Bearer <key>` only.** The v1 docs describe an
`?api_key=` query parameter; it returns `Require api-key` on this build. Both
v1 and v2 paths need the header.

**`POST /api/v2/browser-profile/list` ignores `page_size`** — it returns exactly
one profile per page and echoes `page_size: 1`, with `total_count` present only
on page 1. Paginating it costs N requests against a 1-req/sec limit. Use
`GET /api/v1/user/list?page_size=100` instead; `AdsPowerClient.list_profiles`
does this and normalises `user_id`/`serial_number` to the v2 field names so
callers never see the difference.

**The built-in dynamic proxy (`proxy_soft: adspowerauto`) has no connectivity
here.** Every navigation in such a profile ends at `chrome-error://` with
`ERR_TIMED_OUT`. What works — and what the AdsPower client's own proxy test
exercises — is the **proxy list** under 代理管理, which on this account holds the
100 i-proxy entries (`gateway.i-proxy.com:10000-10099`). Bind one with
`proxyid` at create time.

**Stripe must bypass the proxy.** Residential proxies blacklist payment domains;
without a bypass `checkout.stripe.com` fails with
`ERR_TUNNEL_CONNECTION_FAILED` and the whole payment chain is dead. There is no
Playwright `proxy.bypass` to set under CDP takeover, so it goes in as a Chrome
launch arg at profile start:

```
--proxy-bypass-list=*.stripe.com;stripe.com;*.stripecdn.com;...
```

**Semicolons**, not the commas `app.py`'s `_PROXY_BYPASS` uses — that one is
Playwright's format. Same intent, different syntax; keep the two constants
separate rather than sharing a string.

**`add_init_script` still injects ahead of page scripts after CDP takeover.**
This was the open risk in the design (see `memory: stripe-hcaptcha-blocker` —
Patchright cripples it, which is why payment runs on vanilla Playwright).
Verified end-to-end by `scripts/probe_adspower.py`; keep that assertion in the
probe, because losing it silently breaks hCaptcha token delivery and the failure
surfaces only as payment timeouts.

**An unset `ua_system_version` randomises across *every* OS**, mobile included —
the docs say so explicitly (`不填默认在所有系统中随机`: Android, iOS, Windows,
Mac OS X, Linux). A mobile UA gets mobile GitHub and mobile Stripe, and every
selector in this project is written against the desktop layout, so the run fails
in a way that looks like a page-structure bug. `_fingerprint_config` always sends
`random_ua.ua_system_version`; `ADSPOWER_UA_SYSTEMS` (overridable via
`adspower.ua_systems`) holds Windows 10/11 and Mac OS X 12/13 only.

Sampling 10 profiles created *without* the field returned Windows and macOS every
time. That is randomness landing well, not a guarantee — do not read it as one.
Verified after the fix: 8 fresh profiles, all Windows or macOS.

Windows 7/8 are supported values but excluded on purpose: near-zero install base
makes them a standout fingerprint rather than a blend-in one.

**`stop` on a profile that is not running raises** `Profile is not open`.
`_stop_all` swallows it — a closed profile is exactly the state the caller wants.

**`stop` is asynchronous.** `browser-profile/active` still reports `Active` for
about a second after `stop_profile` returns. Anything asserting "it stopped" must
poll, not check once.

**A running profile cannot be deleted** — `is being used by other users and
cannot be deleted`. `AdsPowerProfilePool._stop_all` stops the batch and waits
before deleting.

---

## Proxy occupancy lives on the server

`proxy-list/list` returns `profile_count` and `related_profile_no` per proxy.
That is the authority for "is this proxy free", and it updates immediately when a
profile is created or deleted.

Do **not** add a local `ProxyRegistry`-style in-memory map for AdsPower proxies.
The environment↔proxy binding is persistent and cross-process; an in-memory view
of it is wrong the moment the process restarts.

When every proxy is already bound, `pick_free_proxy` takes the least-used one and
logs a warning. When the list is empty it **raises** rather than falling back to a
direct connection — a silent direct connection puts every account on the same
host IP, which defeats the isolation and is invisible in the logs.

---

## Reclaim: lazy, serialized, and never touches a busy account

Reclaim runs only when `create_profile` raises `AdsPowerQuotaExceeded`. There is
no background cleaner: deleting a profile discards that account's login session,
so proactive tidying costs sessions we may still need.

Candidates come from `AdsPowerProfileModel.reclaim_candidates`. The ordering key
is **how much login state the profile still holds**, not how "done" the account
is — a registration that never completed leaves an empty profile, so those go
first. Three tiers, oldest `last_used_at` first within each:

| Tier | Condition | Rationale |
|---|---|---|
| 0 | The account row is gone from `accounts` (orphaned mapping) | Nothing left to preserve |
| 1 | `identity_status ∈ (failed, pending, rejected, flagged, banned, suspended)` | GitHub side is dead — useless on *every* platform |
| 2 | Identity usable, **and** the mailbox has at least one `platform_accounts` row, **and** all of them are terminal | Every platform that was started is finished |

Profiles are keyed by **email, not by (platform, email)** — see
[Multi-Platform](./multi-platform-guidelines.md) for why splitting them is a net
loss. Only the predicate is platform-aware.

Three details in tier 2 that are easy to get wrong, each of which deletes a
browser someone is using:

- Use `NOT EXISTS (… non-terminal row …)`, **not** `status IN (terminal set)`.
  With one platform finished and another still running, the `IN` form calls the
  profile reclaimable.
- Also require `EXISTS (… any row …)`. Without it `NOT EXISTS` is vacuously true
  for a mailbox not yet onboarded anywhere — precisely the freshly registered
  accounts whose GitHub session is about to be used. This replaces the older
  "`registered` is deliberately absent from the list" rule; same protection,
  stated as a condition instead of an omission.
- Tier 0 needs a `LEFT JOIN accounts`. It used to be an inner join, so orphaned
  mappings were joined away, never became candidates, and their remote profiles
  occupied quota forever — the production database had one such profile eating
  1 of the 12 slots.

> **2026-08-03 incident.** The first version of this list omitted
> `failed`/`pending`/`rejected`. Eleven accounts whose registration had failed
> held profiles that nothing could reclaim, quota sat at 11/12, and every
> subsequent account died on `配额已满且无可回收`. Worse, `signup_one` converted
> that `AdsPowerQuotaExceeded` into `outcome='error'`, so the caller marked each
> account `failed` — dropping 25 freshly imported accounts out of the `imported`
> pool permanently for a failure that never touched them. Two rules came out of
> it: reclaim must cover every status whose profile holds nothing, and an
> infrastructure failure must never be recorded as an account-level outcome.
> `AdsPowerError` now propagates out of `signup_one` untouched, and both
> pipelines treat it as "stop the whole run", not "this account failed". The pool then drops any account that `is_busy(email)`
reports as in use — wired to `AccountRegistry.is_claimed`. **Deleting a profile a
worker is currently driving makes that worker's browser vanish mid-run.**

The whole create/reclaim/retry path is under one `RLock`. Without it, concurrent
workers that both hit the quota each reclaim and each retry, and the profile A
freed gets taken by B before A retries — both keep failing while both keep
deleting. Serializing costs a few seconds of create-time concurrency and removes
the livelock.

Local mappings are deleted **after** the remote delete succeeds. Reversing the
order leaves profiles that nothing maps to and nothing will ever delete,
permanently eating quota.

When no candidate survives filtering, the `AdsPowerQuotaExceeded` propagates; the
pipeline marks that one account failed and moves on. It must not retry forever.

---

## Eager release when an account is deleted

Reclaim being lazy is right for accounts that *finished*; it is wrong for accounts
the user *deleted*. Deleting an account is terminal — the profile holds a session
nobody will ever want again — so `POST /api/accounts/delete` calls
`AdsPowerProfilePool.release_many(emails)` **before** removing the DB rows, and the
slot comes back immediately instead of waiting for the next quota collision.

`release_many` is not `for e in emails: release(e)`. `release` runs `_stop_all`,
which sleeps 1.5s waiting for AdsPower's async state flip; per-account that is
30 seconds for a 20-account delete and the request times out in the browser. One
stop batch plus one delete batch pays that 1.5s once.

Two invariants carry over from reclaim, and one is new:

- Local mappings are cleared **only after** the remote delete succeeds. On
  `AdsPowerError` the mapping stays and the emails come back in `failed`.
- `is_busy(email)` accounts keep their profile — same reason as reclaim. They are
  returned in `skipped_busy`, **and the account row is still deleted**: the user's
  intent to delete is not vetoed by a running job. The mapping becomes an orphan,
  which tier 0 reclaims once the worker finishes. This is the one place that
  creates orphans on purpose.
- Release is **best-effort and never blocks deletion**. AdsPower disabled, client
  unreachable, delete failing — all are swallowed by `_release_adspower_for` in
  `src/api/routes.py`, which returns `{released, skipped_busy, failed, reason}`
  for the UI. An external dependency being down must not make account cleanup
  impossible.

The response's `adspower` block has to be surfaced in the UI. Silently swallowing
`skipped_busy`/`failed` tells the user quota was freed when it wasn't; they only
find out at the next `配额已满`, with nothing linking it back to the delete.

---

## Client-side rate limiting is not optional

0–200 profiles allows 2 req/sec; `user/list`, `proxy-list/*` and a few others are
fixed at 1 req/sec. Exceeding it returns
`{"code":-1,"msg":"Too many request per second, please check"}` — a hard failure,
not a queue.

`AdsPowerClient._throttle` sleeps **while holding the lock** so concurrent callers
genuinely queue. Recording a timestamp without holding the lock would let N
threads pass simultaneously and is the same as no throttle at all.

---

## `BrowserSession` in remote mode

`create_driver_adspower` returns the ordinary `BrowserSession` with
`remote_browser` and `remote_stop` set, so all 50+ driver helpers and the
`opencode_*` page flows work unchanged. `quit()` branches:

- `remote_browser.close()` — **disconnects CDP**. Never `context.close()`: that
  context is the browser's real default context, closing it kills the browser out
  from under AdsPower, whose profile state then stays `Active` over a dead
  process and refuses to start next time.
- No `_kill_chrome_for_profile`. That matches on `--user-data-dir` and AdsPower's
  directory is shared across its profiles — a kill there takes out unrelated
  environments.
- `remote_stop()` runs last and is idempotent (the watchdog may have called it).

Anything constructing `BrowserSession` via `__new__` in tests must set both
fields; `tests/test_close_watchdog.py::_make_session` shows the local-mode shape.

---

## Picking a page after takeover

`contexts[0].pages` contains a `devtools://` page and an initial tab that AdsPower
replaces during startup. Taking `pages[0]` gets a closed target and every call
fails with `Target page, context or browser has been closed`. `_pick_page` skips
`devtools://` and closed pages, and there is a short settle wait before selecting
— that wait is load-bearing, not superstition.

---

## Verifying changes

```bash
python3 -m pytest tests/test_adspower_pool.py -q     # pool + reclaim + remote quit, all faked
python3 scripts/probe_adspower.py --email x@y.z      # real: create/reuse, exit IP, Stripe, init-script
python3 scripts/probe_adspower.py --cleanup          # delete every profile this project created
```

The unit tests use a fake client and never touch the network. Anything about
exit IPs, Stripe reachability, or script injection can only be checked by the
probe against a running AdsPower client — assertions about those belong there.
