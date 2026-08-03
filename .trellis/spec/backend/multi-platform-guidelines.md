# Multi-Platform Guidelines

> How this project runs the same mailbox and the same card pool against several
> target sites without letting them contaminate each other.

---

## The layering

```
src/identity/           who we are        GitHub account supply (reusable OAuth identity)
src/platforms/<slug>/   where we are      site-specific navigation & outcome判定
src/payments/           how we pay        Stripe Checkout form operations
src/browser/            what we drive     browser lifecycle, CDP utils, progress callback
```

Dependencies point downward only. `payments` must never import `platforms` —
that is why the shared progress helper lives in `src/browser/monitor.py` rather
than in the platform layer.

A **platform** is a target site we open accounts on and pay. A platform is
identified by a string slug. The adapter registry in `src/platforms/__init__.py`
is the **single source of truth** for which platforms exist — the database only
ever stores the slug string. There is deliberately no `platforms` table: another
table would be another thing to keep in sync, and the platform list changes with
a code release, not at runtime.

## Adding a platform

1. Write a class satisfying `PlatformAdapter` (`src/platforms/base.py`).
2. Register it in `src/platforms/_bootstrap()`.

That is the whole list. No orchestration code changes — `tests/test_platform_adapter.py`
runs the entire top-up pipeline against a fictional `StubAdapter` to keep it that
way. If someone hardcodes a platform back into the orchestration layer, that file
goes red.

### What belongs in the adapter, and what does not

The interface is deliberately narrow — 7 methods. Earlier drafts had 12; the
extra five (`auth_entry_urls`, `click_oauth_entry`, `balance_url`,
`start_payment`, `detect_payment_outcome`) were only ever called from *inside*
`ensure_session` / `top_up` / `subscribe`. Exposing them would force the next
platform to decompose its flow the way opencode happens to decompose its own —
the opposite of what an abstraction is for. Keep them private.

The one exception is `read_balance_from_current_page`, which the API layer calls
directly while a human has a browser open.

Per-platform tuning lives on the adapter as plain attributes, not in environment
variables: `max_card_attempts`, `recharge_skip_balance`, `default_topup_amount`.
Risk thresholds genuinely differ between sites.

## `PaymentResult.outcome` — a hard contract

| outcome | Card consumed? | Meaning |
|---|---|---|
| `success` | yes (marked `paid`) | Payment went through |
| `failed` | yes | Explicit decline → invalidate if never succeeded **on this platform**, else 24h cooldown |
| `needs_captcha` | **no** | Account-level risk block. Stop immediately, do not try more cards |
| `error` | **no** | Page/infrastructure failure *before* payment |
| `unknown` | **no** | Submitted, no confirmation, no clear signal |
| `dry_ready` | **no** | Rehearsal: card filled, not submitted |

The three "no" rows are non-negotiable, and `OUTCOMES_KEEPING_CARD` /
`PaymentResult.keeps_card` exist to make that checkable. A new adapter that
reports a network blip as `failed` will permanently invalidate good cards, and
that is not reversible.

## What is isolated per platform, and what is not

Not everything should be split. The rule is: **does this describe the card/identity
itself, or what happened at one merchant?**

| Thing | Scope | Why |
|---|---|---|
| Account status | per platform (`platform_accounts.status`) | Recharged on A says nothing about B |
| GitHub signup/ban outcome | **global** (`accounts.identity_status`) | A flagged GitHub account cannot authorize OAuth anywhere |
| Card `bound` / `invalid` / `paid` | per platform (`card_platform_state`) | All three are merchant-specific verdicts |
| Card `expired` | **global** (`card_pool.status`) | Expiry is a property of the card |
| `valid_cards` membership | per platform | "Proved usable" is proved against one merchant |
| 3DS / rate cooldown | per platform | 3DS is decided by merchant + issuer together |
| `PaymentCardRegistry._used` | per platform | Pure round-dedup heuristic; irrelevant across sites |
| `PaymentCardRegistry._in_flight` | **global** | Submitting the same card at two merchants at once stacks issuer velocity risk. The issuer sees the card, not our platform |
| `ProxyRegistry` | **global** | An exit IP is a physical resource |
| AdsPower browser profile | **global, per email** | See below |
| `[Stripe字段错误]` cards | **global** | The card data itself is malformed; it will be malformed everywhere |

### Why AdsPower profiles stay per-email

Tempting to split, wrong to split. A profile exists to preserve cookies, and the
valuable cookie is the **GitHub authorization**, which is shared across every
OAuth platform by construction. Platform sessions do not survive a browser
restart anyway. Splitting by `(platform, email)` divides a hard quota of 12
profiles by the number of platforms in exchange for a short-lived session — a net
loss.

What *does* need to become platform-aware is the reclaim predicate. See
`AdsPowerProfileModel.reclaim_candidates`: three tiers, and two of the conditions
are easy to get subtly wrong.

- Use `NOT EXISTS (… non-terminal row …)`, not `status IN (terminal set)`. With
  two platforms — one finished, one still running — the `IN` form calls the
  profile reclaimable and deletes a browser someone is using.
- Also require `EXISTS (… any row …)`. Without it, `NOT EXISTS` is vacuously true
  for accounts that have not been onboarded anywhere — precisely the freshly
  registered ones whose GitHub session is about to be used.

## Execution model: one platform at a time

`AppState` is a singleton. `is_running`, the counters, the stop flag and all three
in-memory registries are global, so two pipelines running concurrently would clear
each other's claims. `AppState.platform` records which platform the current run
targets; switching platforms in the UI is disabled while a task runs.

Making platforms genuinely concurrent means splitting `AppState` per platform and
fixing `release_all()` being called unconditionally at task teardown (it would
release the other run's proxies). Out of scope so far — do not assume it works.

## Passing the platform around

- API layer: `_req_platform()` in `src/api/routes.py`. Read endpoints fall back to
  `AppState.platform`; pipeline-start endpoints pass `required=True` and return
  400 when it is missing. Guessing is worse than failing — a wrong guess writes
  data to the wrong platform.
- Frontend: injected centrally in `frontend/src/api/index.js`, never by individual
  call sites. A missed call site does not error; it quietly shows another
  platform's data.
- Models: `platform` is a required positional parameter on every card/account
  state method. No defaults — a default is how a missed call site becomes silent
  cross-platform contamination.
