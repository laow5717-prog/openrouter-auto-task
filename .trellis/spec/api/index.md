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
| GET | `/api/status` | Task status, logs |
| POST | `/api/start` | Start batch registration |
| POST | `/api/stop` | Stop running task |
| GET | `/api/card/template` | Download card Excel template |
| POST | `/api/card/upload` | Upload card Excel |
| POST | `/api/card/start` | Start card-driven task |
| GET | `/api/card/status` | Current task card status (paginated) |
| GET | `/api/card/report` | Download card report Excel |
| GET | `/api/card/history` | All card bindings across tasks (paginated) |
| POST | `/api/card/history/export` | Export card history to Excel |
| GET | `/api/accounts` | Account list (paginated) |
| GET | `/api/accounts/<email>/cards` | Cards bound to account |
| POST | `/api/accounts/export` | Export accounts to Excel |

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
