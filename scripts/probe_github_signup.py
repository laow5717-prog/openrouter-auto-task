"""临时侦察脚本（步骤0）：实跑打开 GitHub signup 页，dump 表单 DOM 结构。

产出用于确定 github_signup.py 的选择器；侦察完成后本脚本可删。
不点最终提交，只观察：初始表单 → 填入真实 mail.tm 邮箱触发校验/揭示 → 再次 dump。
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver
from src.services.email import create_temp_email

SIGNUP_URL = "https://github.com/signup?source=form-home-signup&user_email="

DUMP_JS = r"""
() => {
  const pick = el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    id: el.id || null,
    name: el.getAttribute('name'),
    autocomplete: el.getAttribute('autocomplete'),
    placeholder: el.getAttribute('placeholder'),
    ariaLabel: el.getAttribute('aria-label'),
    ariaDescribedby: el.getAttribute('aria-describedby'),
    visible: !!(el.offsetParent || el.getClientRects().length),
    disabled: el.disabled || null,
  });
  const inputs = Array.from(document.querySelectorAll('input')).map(pick);
  const buttons = Array.from(document.querySelectorAll('button, [role=button], input[type=submit]')).map(b => ({
    tag: b.tagName.toLowerCase(),
    type: b.getAttribute('type'),
    id: b.id || null,
    text: (b.innerText || b.value || '').trim().slice(0, 60),
    ariaLabel: b.getAttribute('aria-label'),
    visible: !!(b.offsetParent || b.getClientRects().length),
    disabled: b.disabled || null,
  }));
  // 潜在的错误提示 / 校验状态节点
  const notices = Array.from(document.querySelectorAll('[role=alert], .error, .flash-error, [id*=err], [class*=error], [aria-live]'))
    .map(n => ({ id: n.id||null, cls: n.className, text: (n.innerText||'').trim().slice(0,120), visible: !!(n.offsetParent||n.getClientRects().length) }))
    .filter(n => n.text);
  // Arkose / captcha 迹象
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => ({ id: f.id||null, name: f.getAttribute('name'), src: (f.getAttribute('src')||'').slice(0,120), title: f.getAttribute('title') }));
  const arkose = Array.from(document.querySelectorAll('[id*=arkose i], [class*=arkose i], [id*=funcaptcha i], [id*=captcha i]')).map(a => ({ id:a.id||null, cls:a.className }));
  return { url: location.href, title: document.title, inputs, buttons, notices, frames, arkose };
}
"""


def dump(page, label):
    data = page.evaluate(DUMP_JS)
    print(f"\n========== DUMP: {label} ==========")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main():
    print("创建 mail.tm 邮箱用于侦察...")
    address, mail_pw, token = create_temp_email()
    print(f"侦察用邮箱: {address}")

    session = create_driver(headless=False)
    snapshots = {}
    try:
        session.get(SIGNUP_URL)
        time.sleep(4)
        snapshots['initial'] = dump(session.page, "初始表单")
        try:
            session.page.screenshot(path="/tmp/gh_signup_initial.png")
        except Exception as e:
            print(f"截图失败: {e}")

        # 尝试定位 email 输入并填入，触发校验/逐字段揭示
        email_sel = None
        for sel in ['#email', 'input[name=email]', 'input[autocomplete=email]', 'input[type=email]']:
            if session.page.query_selector(sel):
                email_sel = sel
                break
        print(f"\nemail 选择器命中: {email_sel}")
        if email_sel and address:
            session.page.fill(email_sel, address)
            session.page.dispatch_event(email_sel, 'blur')
            time.sleep(4)
            snapshots['after_email'] = dump(session.page, "填入邮箱后")
            try:
                session.page.screenshot(path="/tmp/gh_signup_after_email.png")
            except Exception:
                pass

        print("\n侦察完成。浏览器保持打开 20 秒供肉眼确认...")
        time.sleep(20)
    finally:
        # 落盘快照供后续写 research 文档
        with open("/tmp/gh_signup_probe.json", "w") as f:
            json.dump({"email": address, "snapshots": snapshots}, f, ensure_ascii=False, indent=2)
        print("快照已存 /tmp/gh_signup_probe.json")
        close_driver(session)


if __name__ == "__main__":
    main()
