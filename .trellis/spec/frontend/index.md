# Frontend Development Guidelines

> Vue 3 + Vite + Pinia SPA served by Flask backend.

---

## Architecture

| Layer | Tech | Notes |
|-------|------|-------|
| Framework | Vue 3 (Composition API, `<script setup>`) | No Options API |
| Build | Vite | Output to `../static/`, served by Flask |
| Router | vue-router (hash history) | Hash mode avoids server-side routing |
| State | Pinia | `stores/app.js` (polling), `stores/settings.js` (localStorage) |
| API | `api/index.js` | Thin wrappers: `get()`, `post()`, `postFile()`, `postBlob()` |

## Directory Structure

```
frontend/
├── src/
│   ├── api/index.js        # All API calls
│   ├── components/          # Reusable: FilterBar, Pagination, Modal, Icon, SidebarControls, CardEntry
│   ├── router/index.js      # Hash router, lazy-loaded routes
│   ├── stores/              # Pinia stores
│   ├── styles/global.css    # CSS variables, base styles, table/stat-card styles
│   └── views/               # Page components (Dashboard, CardMode, Accounts, CardHistory)
├── vite.config.js
└── package.json
```

## Conventions

### Adding a New Page

1. Create `views/NewPage.vue`
2. Add route in `router/index.js` (lazy-loaded)
3. Add sidebar link in `App.vue` nav-menu
4. Add title mapping in `App.vue` `titleMap`
5. Add API functions in `api/index.js` if needed

### Table Pages Pattern

All list pages follow the same structure:
- Stats grid (optional) → FilterBar → table → Pagination
- Use `white-space: nowrap` on tables with many columns; wrap in `overflow-x: auto`
- Scoped CSS with table-specific class (e.g., `.acc-table`, `.history-table`)

### Modal with `wide` Prop

`<Modal>` accepts optional `wide` boolean prop for 720px width (default 560px).

### Icons: `components/Icon.vue`

Nav and action buttons use `<Icon name="..." size="18" />` — a stroke-based 24×24 SVG
library keyed by name (`bolt`, `play`, `stop`, `monitor`, `terminal`, `dashboard`, `cards`,
`bind`, `accounts`, `history`, `wallet`, …). Add new glyphs to the `ICONS` map in that file
rather than inlining raw `<svg>` or emoji `<span>` in views. Unknown names render nothing.

### Form Params: persist via settings store, not local refs

Automation forms (e.g. `Workbench.vue`) bind inputs directly to `useSettingsStore()` fields
(`dailyBindGroupId`, `dailyPaymentGroupId`, `cfPassword`, `captchaApiKey`, `maxBindableCards`),
call `settings.save()` on submit to write localStorage, so values auto-restore next visit.
When a group dropdown has exactly one option and the stored value is empty, auto-select it.
`save()` writes empty strings too (so clearing a field persists) — don't gate writes on truthiness.

### Build & Deploy

```bash
cd frontend && npm run build   # outputs to ../static/
# Restart Flask server to pick up new static files
```

Dev proxy: `vite.config.js` proxies `/api` and `/video_feed` to Flask on port 5000.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [index.md](./index.md) | Architecture overview | Active |
