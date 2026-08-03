# Directory Structure

> How backend code is organized in this project.

---

## Overview

The layout encodes one rule: **anything that names a specific site belongs to a
platform adapter, not to shared code.** That rule is what makes the same mailbox
pool and card pool usable against several targets. See
[Multi-Platform](./multi-platform-guidelines.md) for the reasoning.

---

## Directory Layout

```
src/
├── api/routes.py         Flask endpoints. Resolves the target platform per request
│                         (`_req_platform`) and hands it down; holds no site knowledge
├── web/
│   ├── app.py            AppState: the daily pipelines, worker scheduling, the three
│   │                     runtime registries, AdsPower profile factory
│   └── worker.py         WorkerState + AccountRegistry / PaymentCardRegistry / ProxyRegistry
├── models/               One module per table. Every card/account state method takes
│                         `platform` as a required parameter
├── services/
│   ├── registration.py   Top-up orchestration — platform-agnostic skeleton
│   ├── github_signup_service.py, hotmail_inbox.py, email.py   identity supply
│   └── card.py, captcha.py, adspower.py                       shared services
├── platforms/            ← target sites
│   ├── base.py           PlatformAdapter protocol, SessionResult, PaymentResult
│   ├── __init__.py       Adapter registry (the source of truth for which platforms exist)
│   └── opencode/         login.py / billing.py / subscribe.py + the adapter class
├── payments/
│   └── stripe_checkout.py  Stripe Checkout form operations. Site-agnostic
├── identity/             GitHub account supply (reusable OAuth identity)
├── browser/
│   ├── driver.py         Browser lifecycle, safe interaction primitives, CDP/DOM utils.
│   │                     **No site URLs or selectors** — that is the whole point
│   ├── adspower_driver.py  Fingerprint-browser profile pool and CDP takeover
│   ├── github_signup.py    GitHub's own pages (identity layer, platform-agnostic)
│   └── monitor.py          Progress callback shared by platforms and payments
└── config.py, utils.py   Config loading; card status constants, terminal-status sets,
                          failure attribution whitelist
```

Dependency direction is downward only:

```
api / web  →  services  →  platforms  →  payments  →  browser
                              ↘  identity  ↗
```

`payments` importing `platforms` would invert the layering — that is why the
shared progress helper sits in `src/browser/monitor.py` rather than in a platform
module.

---

## Module Organization

**Adding a target site** → new package under `src/platforms/<slug>/`, implement
`PlatformAdapter`, register it. Nothing above that layer changes;
`tests/test_platform_adapter.py` enforces this by running the whole top-up
pipeline against a fictional adapter.

**Adding a payment provider** → new module under `src/payments/`. Adapters
compose it; it must not know which platform is calling.

**Adding a table** → new module under `src/models/`, plus a migration in
`database.py`. If the table records anything site-specific it needs a `platform`
column and every query must filter on it — check the isolation table in the
multi-platform guidelines before deciding, because a few things (card expiry,
in-flight card exclusion, proxies, browser profiles) are deliberately global.

**Where does a new piece of logic go?** In order:

1. Does it mention a specific site's URL, selector, or copy? → that platform's package.
2. Is it about filling or submitting a payment form? → `src/payments/`.
3. Is it about acquiring or logging into an identity (GitHub, mailbox)? → `src/identity/` or `src/services/`.
4. Is it about driving a browser at all? → `src/browser/`.
5. Otherwise it is orchestration → `src/services/` or `src/web/app.py`.

---

## Naming Conventions

- Models are `<table_singular>.py` exporting `<Table>Model` (`platform_account.py`
  → `PlatformAccountModel`).
- Platform packages are named by slug, and the slug is what the database stores.
- Inside a platform package, modules are named by flow (`login` / `billing` /
  `subscribe`), not by page.
- A leading underscore marks "private to this module". Several such helpers are
  imported across modules within the same layer (e.g. `_stripe_frame`); that is
  tolerated inside a layer but never across one.

---

## Examples

- `src/services/registration.py` — orchestration with zero site knowledge; the
  clearest example of what the abstraction bought.
- `src/platforms/opencode/__init__.py` — an adapter that only wires existing
  modules to the protocol, changing no logic.
- `src/models/card_pool.py` — how a two-table state split is expressed once
  (`_EFF_STATUS`) and reused by every query.

---

## Scripts

`scripts/` holds one-off probes and manual tools, not production paths. They may
import platform modules directly — the "no direct platform imports" rule applies
to `src/` only. Several `probe_*.py` files record how a page behaved when they
were written; treat them as notes, not as maintained code.
