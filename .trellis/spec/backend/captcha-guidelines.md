# Captcha Guidelines

> How solved tokens get delivered to the page, and why the obvious way silently
> fails. Read before touching `src/services/captcha.py` or the Turnstile handling
> in `src/browser/driver.py`.

---

## Delivering a token is not `input.value = token`

Cloudflare's dashboard is a React app. React attaches a `_valueTracker` to each
controlled input; assigning `el.value` directly **also updates the tracker's
recorded value**, so React concludes nothing changed and never fires `onChange`.
The component's state keeps the old (empty) token, and that is what gets
submitted.

The failure is silent end to end: the field looks filled in DevTools, the form
submits, Cloudflare's backend rejects the empty token without an error message,
and no verification email is ever sent. The only symptom upstream is
`等待验证邮件超时` — two layers away from the actual cause.

Delivery requires all of:

```javascript
const nativeSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
nativeSetter.call(el, token);                                  // bypass the tracker
el.dispatchEvent(new Event('input',  {bubbles: true}));        // let React see it
el.dispatchEvent(new Event('change', {bubbles: true}));
```

Plus the widget's own callback where one is exposed.

## Turnstile has no `setResponse`

hCaptcha exposes `hcaptcha.setResponse(widgetId, token)`. **Turnstile does not.**
When the page uses explicit render, the callback is a closure inside
`turnstile.render(el, {callback})` and is unreachable from outside.

The only public delivery path is the `data-callback` attribute:

```javascript
document.querySelectorAll('[data-callback]').forEach(c => {
    const fn = window[c.getAttribute('data-callback')];
    if (typeof fn === 'function') fn(token);
});
```

Do not write code that reads `data-turnstile-id` and expects to do something with
it — there is no API that accepts it. An earlier version of
`_inject_turnstile_token` did exactly that and the block was a no-op.

## Report what was delivered, not just true/false

`_inject_turnstile_token` returns a boolean but logs a breakdown: how many fields
were written, whether the native setter was available, how many events fired,
which callbacks ran. Without it, "token injected" is indistinguishable from
"token written into a field nobody reads".

## Never swallow a captcha failure

`fill_signup_form` used to ignore `_handle_inline_turnstile`'s return value and
click submit anyway, then return `True`. A failed challenge became an
un-diagnosable "no verification email" 120 seconds later.

If the challenge did not pass, do not submit. Fail where the failure is.

Related: `_report_signup_page_error` dumps whatever error text Cloudflare put on
the page after submit. Cloudflare usually *does* say why it refused; nobody was
looking.

## Anti-detection constraints (do not relax)

These are recorded from failures, not preferences:

- **No CDP for shadow-DOM piercing on the registration path.** Creating a CDP
  session or sending DOM commands breaks Patchright's stealth and triggers
  "There was a problem with verification" (`driver.py:932-934`). The Turnstile
  iframe lives in a *closed* shadow root, so Playwright locators cannot reach it
  either — blind coordinate clicking is the only option, which is why token
  injection matters.
- **Never register `page.on("console")`** — it forces `Runtime.enable`, the
  primary signal Cloudflare uses to detect CDP-controlled browsers.
- **Never use Playwright's `locale` option** — it goes through
  `Emulation.setLocaleOverride`. Set language via Chrome's own `--lang` flag and
  the profile's `intl.accept_languages` instead.

## Payment-flow hCaptcha (Stripe Checkout) — subscribe **and** recharge share one shape

Stripe Checkout (both the "Subscribe to Go" flow and the zen-console recharge flow) can
pop an enterprise / invisible hCaptcha at Pay time. Solving it is fundamentally different
from the registration-path Turnstile above, and the two payment flows use an **identical**
mechanism — implement/verify them together:

- **Requires the vanilla Playwright driver** (`create_driver_vanilla`), NOT Patchright.
  Patchright neuters `add_init_script` / CDP pre-injection for stealth, so the
  `window.hcaptcha.render/execute` hook never installs and the solved token can't be
  delivered into Stripe's cross-origin `HCaptchaInvisible.html` OOPIF. The payment flows
  deliberately trade stealth for injection. `create_driver_vanilla` reuses the **same**
  `data/profiles/<email>` dir as `create_driver`, so the logged-in profile carries over.
- **Install the hook before navigating** to the checkout page:
  `init_solver(key, server)` → `install_hcaptcha_hook(session)` (both flows do this right
  after creating the vanilla session, before login/checkout).
- **Solve inside the result-detection loop, 3DS-first**: once a 3DS challenge is seen,
  never go back to solving hCaptcha (3DS appearing means captcha already passed — otherwise
  the always-present invisible-hCaptcha checkbox iframe gets re-detected forever). When no
  3DS, call `solve_hcaptcha` up to **3** times; after 3 failures return `needs_captcha`
  (account-level risk control — switch card / retry later, do not sit and wait).
  See `opencode_subscribe.detect_subscribe_result` and `opencode_billing.detect_payment_result`
  — they are deliberately the same structure.
- **Balance/余额 is the authoritative success signal** for recharge (`_balance_grew` first
  each loop); `detect_subscribe_result` uses "left checkout & fell back to opencode" instead.
- Default solver is **Multibot** (`api.multibot.cloud`); `init_solver` also accepts
  `2captcha.com`. Multibot needs its own param names (`isInvisible`/`enterprise`/`data`) —
  it is dispatched through `captcha._multibot_hcaptcha`, not the twocaptcha library.
- If no `captcha_api_key` is configured, `is_available()` is False → no hook, no solve, and
  the flow degrades to the old behaviour (detect hCaptcha, prompt for manual Verify, time
  out to `needs_captcha`). Keep that fallback intact.

## Testing captcha injection

Token injection must be tested against a **real DOM** — the thing under test is
React's value tracker, which a mocked DOM cannot reproduce. See
`tests/test_turnstile_injection.py`.

**`page.set_content()` does not execute `<script>` tags.** Listeners and callbacks
written inline in the HTML are never installed, so assertions about them pass
vacuously against a page where nothing was wired up. Install test scaffolding via
`page.evaluate()` instead.

When an assertion covers something subtle like the native setter, verify the test
actually fails without the fix (revert the implementation, watch it go red, put it
back). Two of these tests were confirmed that way.
