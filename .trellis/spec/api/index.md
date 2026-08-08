# API Layer Guidelines

> Flask Blueprint API routes at `/api/*`.

---

## Architecture

- All routes in `src/api/routes.py` under `api` Blueprint
- Access app state via `get_app_state()`, database via `get_db()`, models via `get_models()`
- Models dict: `{ 'account': AccountModel, 'card_binding': CardBindingModel }`

## Pagination Contract

All paginated endpoints follow the same pattern:

**Request params**: `page` (int, default 1), `page_size` (int, default 20), plus optional filters.

**Response**:
```json
{
  "data": [...],
  "total": 123,
  "page": 1,
  "page_size": 20,
  "summary": {}  // optional
}
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Task status, aggregate logs, per-worker overview |
| GET | `/api/workers/<id>/logs` | Incremental per-worker logs (`?index=`) |
| POST | `/api/start` | Start batch registration |
| POST | `/api/stop` | Stop running task |
| GET | `/api/card/template` | Download card Excel template |
| POST | `/api/card/upload` | Upload card Excel |
| POST | `/api/card/start` | Start card-driven task |
| POST | `/api/card/start-from-group` | Start card-driven task from a bind card group |
| POST | `/api/daily/start` | Start daily one-click pipeline (rebind → register → recharge) |
| GET | `/api/card/status` | Current task card status (paginated) |
| GET | `/api/card/report` | Download card report Excel |
| GET | `/api/card/history` | All card bindings across tasks (paginated) |
| POST | `/api/card/history/export` | Export card history to Excel |
| GET | `/api/accounts` | Account list (paginated) |
| GET | `/api/accounts/<email>/cards` | Cards bound to account |
| POST | `/api/accounts/export` | Export accounts to Excel |
| GET | `/api/reports/recharge` | Recharge report: today KPI + range summary + daily trend + per-account ranking |

## Recharge Statistics Contract (报表口径)

Every aggregate that shows **money** — `/api/reports/recharge`, and the
`recharge_today` / `recharge_total` fields on `/api/accounts` — obeys the same four
rules. They are implemented once, in the report section of
`src/models/recharge_log.py`; do not re-derive them at a call site.

1. **Amounts count only `status='success'`.** Failed/pending rows appear in
   counts and success-rate, never in a dollar figure.
2. **Always filtered by `platform`.** Same rule as the rest of that model; the two
   platforms have disjoint account and card pools, so a cross-platform total has
   no operational meaning.
3. **Dates compare via `DATE(created_at)`** against `'YYYY-MM-DD'` params.
   `created_at` is written with `datetime('now','localtime')`, so "today" is
   `DATE(created_at)=DATE('now','localtime')`. Comparing a bare date against a
   full timestamp string silently drops the whole current day — the page just
   looks like nobody recharged.
4. **Distinct card/account counts are computed over the `card_display IS NOT NULL
   AND != ''` success subset**, with cards deduped by the **whole** number
   (spaces stripped), not the last 4. `count_success_by_last4` uses last-4 only to
   tolerate legacy masked strings; reusing that here would merge distinct cards
   sharing a suffix. The tradeoff is the reverse: legacy masked rows
   (`'•••• 1234'`) count as their own card, so early dates can read high.

Two structural consequences worth knowing before editing:

- `COUNT(DISTINCT …)` cannot share a query with the `CASE WHEN status=…` amount
  split — the distinct counts need `status='success'` in the `WHERE`. Hence the
  deliberate two-query shape in `_amount_counts` / `_distinct_counts`.
- The **已核销 vs 在用** split (`identity_status='retired'`) is done in Python in
  the route, over the **full** account aggregate, then the ranking is truncated
  for display. Splitting after truncation makes `verified.amount + active.amount`
  come out under `summary.total_amount` with nothing on screen explaining why.

`/api/recharge-logs` (the raw log list) is **not** covered by this contract — it
still ignores `platform` and returns cross-platform rows. Changing it would
silently alter a page users already read; treat it as a separate decision.

## Background Task Contract (long-running automation)

All automation endpoints (`/api/start`, `/api/card/start*`, `/api/accounts/recharge`,
`/api/daily/start`) share one global lock and one worker-thread lifecycle. Follow it exactly
or the app deadlocks the next task.

> **Note (07-26, recharge hCaptcha + archive)**: the daily **recharge** pipeline is now
> recharge-only: `run_daily_pipeline(group_id, login_password, captcha_api_key,
> captcha_server='api.multibot.cloud')`. Both the daily recharge start endpoint and
> `/api/accounts/recharge` accept `captcha_api_key` + `captcha_server` (Multibot default) so
> the payment page's hCaptcha is auto-solved (see
> [captcha-guidelines.md → Payment-flow hCaptcha](../backend/captcha-guidelines.md)).
> Account selection excludes `banned` **and** `archived`; accounts whose **live** balance
> ≥ $20 (env `OPENCODE_RECHARGE_SKIP_BALANCE`) are set to `archived` and skipped (never
> archive from the possibly-stale DB balance). The three-stage `/api/daily/start`
> (rebind → register → recharge) contract documented further down predates the 07-23
> recharge rework and is **stale** — treat the recharge-only signature above as current.

The `is_running` lock still admits **one task at a time**. Parallelism happens
*inside* the daily pipeline, across browser workers — it does not let two
endpoints run concurrently. See
[concurrency-guidelines.md](../backend/concurrency-guidelines.md).

### Status payload and backward compatibility

`/api/status` keeps every legacy top-level field (`is_running`,
`current_action`, `success`, `fail`, `total_inventory`, `logs`). Parallel
execution is exposed as **additive** fields only:

```json
{
  "parallel_mode": true,
  "workers": [{"id": "W1", "current_action": "…", "busy": true, "log_seq": 142}]
}
```

- `logs` stays the aggregate stream. In parallel mode each line is prefixed
  `[Wn]`; in serial mode it is byte-identical to before.
- `workers` is truncated to the **current** concurrency, not every worker ever
  created — otherwise dropping `max_workers` back to 1 still looks parallel.
- `/api/workers/<id>/logs` falls back to the primary worker for unknown ids
  instead of 404ing; the frontend may request an id that just disappeared.
- `/video_feed` without `?worker=` serves the primary worker, so the old URL
  keeps working.

### Signatures

- Route: reject when busy, validate params, then spawn a daemon thread:
  ```python
  if state.is_running:
      return jsonify({"error": "有任务正在运行"}), 400
  threading.Thread(target=state.run_daily_pipeline,
                   args=(bind_group_id, payment_group_id, cf_password,
                         max_bindable_cards, captcha_api_key),
                   daemon=True).start()
  ```
- Worker method on `AppState`: `run_daily_pipeline(bind_group_id, payment_group_id, cf_password, max_bindable_cards, captcha_api_key)`.

### `/api/daily/start` request contract

> The three-stage bind→register→recharge shape below (`bind_group_id`,
> `cf_password`, `max_bindable_cards`) is **Cloudflare-era and gone**. opencode
> and infron fill the card straight into the payment page; there is no separate
> bind stage. The section further down describing three stages is stale for the
> same reason — read `AppState.run_daily_pipeline` for the real flow.

| Field | Type | Required | Note |
|-------|------|----------|------|
| `group_id` | int/str | yes | Payment card-pool group |
| `platform` | str | yes | Adapter slug. Each platform has its own run context and its own `is_running` gate |
| `login_password` | str | no | Overrides each account's own password; normally left empty |
| `captcha_api_key` | str | no | Solver key for the payment page's hCaptcha |
| `captcha_server` | str | no | Default `api.multibot.cloud`; pass `2captcha.com` to switch |
| `amount_min` / `amount_max` | int | no | Per-charge random range, in whole dollars. Default from `cfg.recharge` |
| `balance_cap` | number | no | Switch accounts once one reaches this balance. Default from `cfg.recharge` |

The three policy fields are parsed by `_recharge_cfg_from(data)`, shared with
`/api/accounts/recharge` so the two endpoints cannot drift. It returns a **new**
`RechargeConfig` — never mutates the global `cfg.recharge`, because two
platforms run concurrently against the same process-wide singleton.

Success: `200 {"status": "started", "usable_cards": <int>, "accounts": <int>,
"registerable_accounts": <int>, "reusable_accounts": <int>, "group_name": <str>,
"amount_min": <int>, "amount_max": <int>, "balance_cap": <float>}`. The policy is
echoed back so the UI can show what actually took effect rather than what was typed.

### Admission gate: count all three account pools

The gate must mirror, condition for condition, the three pools
`run_daily_pipeline._try_claim()` actually draws from. It rejects only when **all
three** are empty:

| Pool | Predicate | Pipeline counterpart |
|------|-----------|----------------------|
| Rechargeable | has `login_password`, identity & platform status non-terminal | `_payable_now()` |
| Pending registration | `identity_status == 'imported'` **and** `_hotmail_for_account(a)` resolves | `_registerable_imported()` |
| Reusable | platform status `recharged` and balance below `balance_cap` | `_reusable_recharged()` |

Two failure modes, both expensive to diagnose:

- **Gate stricter than pipeline.** Freshly imported accounts have no
  `login_password` — that password is written *by* the signup flow. Testing them
  with the rechargeable predicate rejects the single most common opening scenario
  ("I just imported a batch of mailboxes"), even though the pipeline's own
  make-up-the-shortfall path (register GitHub → log in → recharge) handles it
  fine. Shipped as a bug on 08-05; fixed in `tests/test_daily_start_gate.py`.
- **Gate looser than pipeline.** Dropping the `_hotmail_for_account` check would
  admit `imported` accounts the pipeline cannot claim (no verification-code
  source). The task starts, burns one empty round, converges. Users read that as
  "started but did nothing" — worse than a 400.

`_registerable_imported()` additionally excludes `done` (this run's terminated
set). The gate runs before the run, where `done` is empty, so it omits that term.

### Validation & Error Matrix

| Condition | Response |
|-----------|----------|
| `state.is_running` truthy for **that platform** | `400 {"error": "<platform> 已有任务在运行"}` |
| missing `group_id` | `400 {"error": "未指定卡池分组"}` |
| group id not found | `404 {"error": "卡池分组不存在"}` |
| missing `platform` | `400 {"error": "未指定平台"}` |
| `amount_min > amount_max` | `400` naming both bounds |
| amount outside `RechargeConfig.AMOUNT_FLOOR..AMOUNT_CEILING` | `400` |
| non-numeric amount / `balance_cap <= 0` | `400` |
| no selectable cards in the group | `400 {"error": ...无事可做}` |
| all three account pools empty (rechargeable / pending-registration / reusable) | `400 {"error": ...无事可做}` |
| otherwise | `200 started` |

Policy validation **rejects with 400 rather than silently clamping**. A user who
configured 20–100 and unknowingly ran something else has a much harder problem
to diagnose than one who got an error. Clamping is reserved for
`RechargeConfig.bounds()`, which is a last-resort guard against a hand-edited
`config.yaml`, not a substitute for validation.

### Worker lifecycle contract (MUST hold)

- **Enter**: `is_running=True; stop_requested=False; _patch_prints()`.
- **Cooperative stop**: never `quit()` the driver from the request thread (Patchright sync API is
  not thread-safe and hangs). `/api/stop` only sets `stop_requested` + stops screenshots. Each
  worker checks `stop_requested` at every account boundary; `_monitor` raises `InterruptedError`
  mid-step, which bubbles to the service's `finally: close_driver`.
- **`finally` (always)**: `clear_active_driver(); _stop_screenshot_loop(); is_running=False`, then
  finalize the task record (`update_counts` + `finish(status='completed' | 'stopped')`). Skipping
  the `is_running=False` reset wedges every future task behind the busy check.

### Multi-stage pipeline pattern

`run_daily_pipeline` runs three stages serially against one shared `daily` task's pending card pool
(consume order: rebind existing accounts → register new accounts → recharge):
- Stage 1a rebind uses the **billing-page real bound count** (`get_bound_card_count`) to decide how
  many to add, never the DB count — avoids over-binding when DB and Cloudflare disagree.
- Stage 2 recharge **re-runs `count_by_emails`** after stages 1a/1b (binding changed the counts),
  then filters `>=1 bound && not has_today_record(email)`.
- Both rebind and recharge loops stop after 3 consecutive failures.

## Excel Export Convention

- Use `openpyxl` with `io.BytesIO` for streaming response
- Chinese column headers (e.g., "邮箱", "卡号", "状态")
- Card data: parse `card_data_json` to show full unmasked info
- Return via `send_file()` with `as_attachment=True`

## Card Data JSON

`card_bindings.card_data_json` stores the full card info as JSON:
```json
{
  "number": "4111111111111111",
  "first_name": "John", "last_name": "Doe",
  "expiry_month": "12", "expiry_year": "2025",
  "cvc": "123",
  "country": "US", "state": "CA", "city": "LA",
  "zip": "90001", "address": "123 Main St", "address2": "",
  "company": ""
}
```

When returning card data to frontend, parse this JSON and flatten fields. No masking — show full card info.

## Common Mistake: Virtual Environment

The server must be started with `.venv/bin/python server.py`, not system Python. Flask and dependencies are installed in the project venv only.
