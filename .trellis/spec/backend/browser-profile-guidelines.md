# Browser Profile Guidelines

> Persistent Chrome profiles live in `data/profiles/<email>`. They accumulate
> state that eventually breaks the browser in ways that look like application
> bugs. Read this before touching `create_driver`, `BrowserSession.quit`, or
> anything that reasons about profile directories.

---

## The white-screen failure

**Symptom**: a browser window opens, the address bar shows the right URL, and
the page never renders. Reported as "白屏".

It has two independent causes, and they compound:

### 1. Corrupt Service Worker cache

`dash.cloudflare.com` is an SPA that registers a Service Worker. The SW
intercepts navigation requests and serves them from cache. When a cache entry is
corrupt — routine after Chrome is killed rather than closed — the SW returns an
empty response. **The URL is correct, the page is blank, and Chrome never
self-heals.**

Observed in the field: a single profile at 695MB, of which `Cache` 248MB,
`Code Cache` 188MB, `Service Worker` 133MB. These directories had never been
cleaned since the profile was created.

`_prune_profile_cache` wipes them when the total crosses
`PROFILE_CACHE_LIMIT_MB` (`src/config.py`, default 200).

### 2. Two Chrome instances on one profile

`_clear_singleton_locks` removes Chrome's `SingletonLock`. If an orphaned Chrome
still holds the `user-data-dir`, the new instance starts anyway and the two
fight over the same leveldb — the renderer never comes up.

`_kill_chrome_for_profile` now reclaims occupants *before* the locks come off,
so "deleting the lock is safe" is verified rather than assumed.

---

## What is safe to delete from a profile

| Directory | Safe? | Why |
|-----------|-------|-----|
| `Default/Cache`, `Default/Code Cache` | ✅ | Rebuilt automatically |
| `Default/Service Worker` | ✅ | Forces SW re-registration — this is the white-screen fix |
| `Default/GPUCache`, `DawnCache`, `ShaderCache` | ✅ | Rebuilt automatically |
| `Default/Preferences` | ✅ (when bloated) | UI settings only; already handled at >10MB |
| **`Default/Cookies`** | ❌ | Login session |
| **`Default/Login Data`** | ❌ | Saved credentials |
| **`Default/Local Storage`** | ❌ | Dash keeps session state here |
| **`Local State`** (profile root) | ❌ | Encryption key for `Cookies` |

Deleting anything in the bottom four logs every account out. `tests/
test_profile_hygiene.py::test_prune_keeps_credentials` is the regression guard —
keep it.

---

## Process lifecycle

**`quit()` must not swallow close failures.** `context.close()` throwing means
the Chrome process is still alive, and `playwright.stop()` immediately after
tears down the driver transport — after that there is no way left to close it.
Silently swallowing produced a leak visible only as a count mismatch in
`server.log` (55 "浏览器初始化成功" vs 48 "正在关闭浏览器").

To audit for leaks:

```bash
grep -c "浏览器初始化成功" server.log
grep -c "正在关闭浏览器" server.log     # should match
ps aux | grep -c "[G]oogle Chrome"
```

**Always leave a grace period before signalling a browser you just closed.**
`context.close()` returns while Chrome is still flushing `Cookies` and
`Local Storage` to disk. Killing it there truncates the write and costs the
login session — a worse outcome than the white screen being fixed.
`_kill_chrome_for_profile(..., grace=5)` on the close path, `grace=0` on the
launch path (orphans there will not exit on their own).

**The close watchdog is two-stage: Chrome first, then the node driver.**
Killing Chrome does not always unblock a hung `context.close()` — the 2026-07-28
daily-recharge incident had Chrome already exited while `close()` still sat
blocked in the Playwright **node driver** (`cli.js run-driver`) for ~300s with
zero log output, indistinguishable from a dead task. The watchdog therefore
kills Chrome at 30s, waits up to 10 more seconds on `_close_finished`, and then
SIGKILLs `_node_pid` (captured at driver creation via
`playwright._impl_obj._connection._transport._proc.pid` — verified on both the
Patchright and vanilla stacks). The flag is set before `watchdog.cancel()`
because cancel is a no-op once the timer has fired; the flag is the only thing
preventing a kill after a slow-but-successful close. Watchdog threads only ever
`os.kill` — never touch Playwright objects from another thread.

**Process lookup uses `ps`, not psutil.** psutil is not a dependency and should
not become one for this. `ps -Ao pid=,command=` does not truncate long command
lines the way `ps aux` does, which matters because the match key is the full
`--user-data-dir=` path. Match to a path boundary or `/a` will hit `/ab`.

Any failure in the lookup returns `[]` and degrades to the old behaviour. This
is best-effort hygiene; it must never prevent a browser from launching.

---

## Relationship to the email exclusion

Reclaiming orphans does **not** relax the account exclusion in
[concurrency-guidelines.md](./concurrency-guidelines.md). A live worker's Chrome
looks exactly like an orphan at the process level, so two workers on one email
would now kill each other's browsers instead of merely corrupting each other's
locks. `AccountRegistry` is still the thing keeping that from happening.
