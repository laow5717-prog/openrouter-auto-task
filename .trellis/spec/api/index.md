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

## Background Task Contract (long-running automation)

All automation endpoints (`/api/start`, `/api/card/start*`, `/api/accounts/recharge`,
`/api/daily/start`) share one global lock and one worker-thread lifecycle. Follow it exactly
or the app deadlocks the next task.

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

| Field | Type | Required | Note |
|-------|------|----------|------|
| `bind_group_id` | int/str | yes | Bind card group id |
| `payment_group_id` | int/str | no | Falsy → `None` → recharge does Top-up only, skips unpaid invoices |
| `cf_password` | str | no | Empty → new accounts get a random password |
| `max_bindable_cards` | int | no | Default 2 |
| `captcha_api_key` | str | no | 2Captcha key |

Success: `200 {"status": "started", "usable_cards": <int>, "group_name": <str>}`.

### Validation & Error Matrix

| Condition | Response |
|-----------|----------|
| `state.is_running` truthy | `400 {"error": "有任务正在运行"}` |
| missing `bind_group_id` | `400 {"error": "未指定绑卡分组"}` |
| bind group id not found | `404 {"error": "绑卡分组不存在"}` |
| no usable cards AND no rechargeable account | `400 {"error": ...无事可做}` |
| otherwise | `200 started` |

> "Nothing to do" guard: if the bind group has zero usable cards, the route must still allow
> start **iff** some account has real bound cards (`count_by_emails >= 1`) and no `has_today_record`
> today — the recharge stage alone is valid work.

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
