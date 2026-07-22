"""诊断：填完表单后，为什么 Create account 按钮不可点。

dump 提交按钮的 disabled/aria-disabled、各字段 aria-invalid 与校验提示文本、
password/username helper 的实时内容，并延长等待观察按钮是否最终 enable。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver
from src.browser import github_signup as gh
from src.services.email import create_temp_email
from src.services.github_signup_service import _gen_username, _gen_password

STATE_JS = r"""
() => {
  const q = s => document.querySelector(s);
  const txt = el => el ? (el.innerText||'').trim().slice(0,160) : null;
  const btn = Array.from(document.querySelectorAll('button[type=submit]')).find(b => /create account/i.test(b.innerText||''));
  const field = sel => { const e=q(sel); return e ? {
    val_len: (e.value||'').length,
    ariaInvalid: e.getAttribute('aria-invalid'),
    ariaDescribedby: e.getAttribute('aria-describedby'),
  } : null; };
  return {
    button: btn ? { disabled: btn.disabled, ariaDisabled: btn.getAttribute('aria-disabled'), text: (btn.innerText||'').trim() } : null,
    email: field('#email'),
    password: field('#password'),
    username: field('#login'),
    emailErr: txt(q('#email-err')),
    passwordHelper: txt(q('#password-helper')),
    usernameHelper: txt(q('#login-err')) || txt(q('#username-helper')) || txt(q('[id*=login][id*=err]')),
    countryBtn: txt(q('#country-dropdown-panel-button')),
    countryHidden: (q('input[name=\"user_signup[country]\"]')||{}).value,
  };
}
"""

def main():
    addr, mail_pw, token = create_temp_email()
    username = _gen_username()
    pw = _gen_password()
    print(f"email={addr} user={username} pw={pw}")
    session = create_driver(headless=False)
    try:
        gh.open_signup(session)
        gh.fill_signup_form(session, addr, pw, username)
        print("\n--- 填完后，每 3s dump 一次按钮/字段状态，共 8 次 ---")
        import json
        for i in range(8):
            st = session.page.evaluate(STATE_JS)
            print(f"[t+{i*3}s] {json.dumps(st, ensure_ascii=False)}")
            time.sleep(3)
        print("\n浏览器保留 15s 观察...")
        time.sleep(15)
    finally:
        close_driver(session)

if __name__ == "__main__":
    main()
